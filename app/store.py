import sqlite3, json
from pathlib import Path
from datetime import datetime, timezone
from app.errors import BusinessError

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    return con

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

def _audit_publish(con,run_id,revision):
    con.execute("CREATE TABLE IF NOT EXISTS publish_log (run_id TEXT NOT NULL, revision INTEGER NOT NULL, published_at TEXT NOT NULL)")
    con.execute('INSERT INTO publish_log(run_id,revision,published_at) VALUES(?,?,?)',(run_id,revision,datetime.now(timezone.utc).isoformat()))

def publish(path,run_id,revision):
    # 单事务发布：审计写入或状态切换任一步中断，整体回滚，旧版结果原样恢复。
    with connect(path) as con:
        row=con.execute('SELECT revision FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
        if row['revision']!=revision: raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
        _audit_publish(con,run_id,revision)
        cur=con.execute("UPDATE runs SET status='已发布',revision=revision+1 WHERE id=? AND revision=?",(run_id,revision))
        if cur.rowcount!=1: raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)
