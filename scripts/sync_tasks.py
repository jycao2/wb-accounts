#!/usr/bin/env python3
"""任务数据「同步（复制）」：把源账号的会话与工作区分组复制一份给目标号。

与早期「转移」（UPDATE user_id 改归属）不同：同步是插入新行、生成新会话 id、
复制正文与 workspace 目录，源账号的数据一行不动 —— 旧账号任务全部保留。

幂等与断点续传：state/sync-ledger.json 记录 (from_uid, sid, to_uid) -> new_id，
重复执行自动跳过已同步项；中途失败重跑只补缺失部分。
执行时机：必须在客户端完全退出后（DB 无写者）调用。
automations（定时任务）刻意不复制：与运行环境绑定，复制过去跑不了。
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path

MSG_COLS = ("sessionId", "sessionID", "session_id")  # 正文行里可能出现的会话 id 字段名


def slug(cwd: str) -> str:
    """D:/foo/bar -> d-foo-bar（与客户端 projects 目录命名一致）。"""
    s = cwd.replace(":", "").replace("\\", "/")
    return s.replace("/", "-").lower()


def _logs(*a):
    print("[sync]", *a, flush=True)


def list_candidates(db_path: Path, uid: str) -> list[dict]:
    """列出某账号可同步的任务（存活会话），供面板勾选。只读。"""
    con = sqlite3.connect("file:%s?mode=ro" % str(db_path).replace("\\", "/"), uri=True)
    try:
        cur = con.cursor()
        spaces = dict(cur.execute(
            "select id,name from spaces where user_id=? and deleted_at=-1", (uid,)))
        out = []
        q = ("select id,coalesce(custom_title,title,'') title,space_id,cwd,"
             "updated_at,transport from sessions where user_id=? and deleted_at is null "
             "order by updated_at desc")
        for sid, title, sp, cwd, upd, tr in cur.execute(q, (uid,)):
            f = Path(db_path).parent / "projects" / slug(cwd or "") / (sid + ".jsonl")
            out.append({
                "id": sid, "title": title or "(无标题)",
                "group": spaces.get(sp) or "",
                "cwd": cwd or "", "updated_at": upd or 0,
                "size": f.stat().st_size if f.is_file() else 0,
                "transport": tr or "local",
            })
        return out
    finally:
        con.close()


def _load_ledger(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_ledger(p: Path, d: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def _copy_body(src: Path, dst: Path, old_id: str, new_id: str) -> int:
    """复制会话正文 jsonl，把每行里的 sessionId 换成新 id。返回目标文件字节数。"""
    with src.open("r", encoding="utf-8", errors="replace") as fin, \
         dst.open("w", encoding="utf-8", newline="\n") as fout:
        for line in fin:
            rstrip_nl = line.endswith("\n")
            body = line[:-1] if rstrip_nl else line
            hit = False
            try:
                o = json.loads(body)
                if isinstance(o, dict):
                    for k in MSG_COLS:
                        if o.get(k) == old_id:
                            o[k] = new_id
                            hit = True
                    if hit:
                        body = json.dumps(o, ensure_ascii=False,
                                          separators=(",", ":"))
            except Exception:
                pass
            if not hit and old_id in body:
                # 兜底：非 JSON 行或嵌套字段，直接替换 id 字符串
                body = body.replace(old_id, new_id)
            fout.write(body + ("\n" if rstrip_nl else ""))
    return dst.stat().st_size


def sync_copy(home: Path, from_uid: str, to_uid: str, selection: list[str] | None = None,
              ledger_path: Path | None = None, progress=None) -> dict:
    """复制 from_uid 的任务到 to_uid 名下。selection=None 表示全部存活会话。

    home: 保险库 HOME（wb-accounts 目录），据其定位 ../workbuddy.db、../projects 等。
    返回 {"ok":bool, sessions, spaces, bytes, skipped, failed, backup}。
    """
    def note(*a):
        (progress or _logs)(*a)

    try:
        if not from_uid or not to_uid or from_uid == to_uid:
            return {"ok": False, "error": "uid 缺失或相同，跳过同步"}
        root = home.parent
        db = root / "workbuddy.db"
        if not db.is_file():
            return {"ok": False, "error": "找不到 %s" % db}
        projects = root / "projects"
        ws_root = root / "workspace" / "sessions"
        ledger_path = ledger_path or (home / "sync-ledger.json")
        ledger = _load_ledger(ledger_path)

        # 1) 整库备份（sqlite backup API，保证一致性）
        bdir = home / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        bpath = bdir / ("workbuddy-db-%s.db" % time.strftime("%Y%m%d-%H%M%S"))
        src = sqlite3.connect(str(db))
        dst = sqlite3.connect(str(bpath))
        src.backup(dst)
        dst.close()
        src.close()
        note("已备份数据库 -> %s" % bpath.name)

        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row
        try:
            cur = con.cursor()
            cols = [r["name"] for r in cur.execute("PRAGMA table_info(sessions)")]
            q = ("select %s from sessions where user_id=? and deleted_at is null"
                 % ",".join(cols))
            rows = [dict(r) for r in cur.execute(q, (from_uid,))]
            if selection is not None:
                want = set(selection)
                rows = [r for r in rows if r["id"] in want]
                missing = want - {r["id"] for r in rows}
                if missing:
                    note("警告：%d 个所选会话不存在或不属于当前账号，已忽略" % len(missing))
            # 幂等过滤：账本里已同步过的直接跳过（分组复制只看真正要复制的那批）
            pending, skip_n = [], 0
            for r in rows:
                if ledger.get("%s>%s:%s" % (from_uid, to_uid, r["id"])):
                    skip_n += 1
                else:
                    pending.append(r)
            rows = pending
            total = len(rows)
            if not rows:
                note("没有需要复制的内容（待同步 %d，已同步过跳过 %d）" % (0, skip_n))
                return {"ok": True, "sessions": 0, "spaces": 0, "bytes": 0,
                        "skipped": skip_n, "failed": [], "backup": str(bpath)}

            # 2) 复制被选中会话引用到的工作区分组（spaces + space_folders）
            #    目标号已有同名分组时复用，避免重复同步产生重复分组
            sp_cols = [r["name"] for r in cur.execute("PRAGMA table_info(spaces)")]
            target_spaces = {row["name"]: row["id"] for row in cur.execute(
                "select id,name from spaces where user_id=? and deleted_at=-1", (to_uid,))}
            sp_map: dict[str, str] = {}
            need_sp = {r["space_id"] for r in rows if r.get("space_id")}
            if need_sp:
                ph = ",".join("?" * len(need_sp))
                for srow in cur.execute("select %s from spaces where id in (%s)"
                                        % (",".join(sp_cols), ph), tuple(need_sp)):
                    sd = dict(srow)
                    exist = target_spaces.get(sd["name"])
                    if exist:
                        sp_map[srow["id"]] = exist
                        continue
                    nid = str(uuid.uuid4())
                    sd["id"], sd["user_id"] = nid, to_uid
                    cur.execute("insert into spaces(%s) values(%s)"
                                % (",".join(sp_cols), ",".join("?" * len(sp_cols))),
                                [sd[c] for c in sp_cols])
                    for frow in cur.execute(
                            "select path,sort_order from space_folders where space_id=?",
                            (srow["id"],)):
                        cur.execute("insert or replace into space_folders values(?,?,?)",
                                    (nid, frow["path"], frow["sort_order"]))
                    target_spaces[sd["name"]] = nid
                    sp_map[srow["id"]] = nid
                    note("复制分组：%s" % sd.get("name", ""))
                con.commit()

            # 3) 逐条复制会话：每条成功即提交，失败只回滚当前这一条
            ok_n = byte_n = 0
            failed: list[dict] = []
            for i, r in enumerate(rows, 1):
                sid = r["id"]
                key = "%s>%s:%s" % (from_uid, to_uid, sid)
                try:
                    nid = str(uuid.uuid4())
                    r["id"] = nid
                    r["user_id"] = to_uid
                    if r.get("space_id") in sp_map:
                        r["space_id"] = sp_map[r["space_id"]]
                    for k in ("agent_dirty", "agent_dirty_at", "agent_last_synced",
                              "verified_at", "unread"):
                        if k in r:
                            r[k] = 0 if k in ("agent_dirty", "unread") else None
                    cur.execute("insert into sessions(%s) values(%s)"
                                % (",".join(cols), ",".join("?" * len(cols))),
                                [r[c] for c in cols])
                    urow = cur.execute("select used,size,updated_at,credit_json "
                                       "from session_usage where session_id=?", (sid,)).fetchone()
                    if urow:
                        cur.execute("insert or replace into session_usage values(?,?,?,?,?)",
                                    (nid, urow["used"], urow["size"],
                                     urow["updated_at"], urow["credit_json"]))
                    body = projects / slug(r["cwd"] or "") / (sid + ".jsonl")
                    if body.is_file():
                        nb = _copy_body(body, projects / slug(r["cwd"]) / (nid + ".jsonl"),
                                        sid, nid)
                        byte_n += nb
                    wsd = ws_root / sid
                    if wsd.is_dir():
                        shutil.copytree(wsd, ws_root / nid, dirs_exist_ok=True)
                    ledger[key] = {"new_id": nid, "at": int(time.time())}
                    _save_ledger(ledger_path, ledger)
                    con.commit()
                    ok_n += 1
                    note("(%d/%d) 已同步：%s" % (i, total, (r.get("title") or sid)[:24]))
                except Exception as e:  # noqa: BLE001
                    con.rollback()
                    failed.append({"id": sid, "error": "%s: %s" % (type(e).__name__, e)})
                    note("(%d/%d) 失败：%s — %s" % (i, total, sid[:8], e))

            # 4) 清两个号的纯缓存（侧边栏快照/启动路由），让客户端重建列表
            cleared = 0
            for u in {from_uid, to_uid}:
                d = root / u
                for name in ("sidebar-list-snapshot.json", "startup-route.json"):
                    try:
                        (d / name).unlink()
                        cleared += 1
                    except FileNotFoundError:
                        pass
            note("同步完成：复制 %d 条 / 跳过 %d / 失败 %d，正文 %.1f MB，清缓存 %d"
                 % (ok_n, skip_n, len(failed), byte_n / 1048576, cleared))
            return {"ok": not failed, "sessions": ok_n, "spaces": len(sp_map),
                    "bytes": byte_n, "skipped": skip_n, "failed": failed,
                    "backup": str(bpath), "cache_cleared": cleared}
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _logs("同步失败：%s: %s" % (type(e).__name__, e))
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


if __name__ == "__main__":
    ap = __import__("argparse").ArgumentParser(description="任务数据同步（复制）预览")
    ap.add_argument("--uid", required=True)
    ap.add_argument("--home", default=str(Path.home() / ".workbuddy" / "wb-accounts"))
    a = ap.parse_args()
    cands = list_candidates(Path(a.home).parent / "workbuddy.db", a.uid)
    print(json.dumps({"count": len(cands),
                      "total_mb": round(sum(c["size"] for c in cands) / 1048576, 1)},
                     ensure_ascii=False))
