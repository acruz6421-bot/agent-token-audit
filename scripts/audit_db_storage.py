#!/usr/bin/env python3
"""Diagnostico de armazenamento fisico do state.db do Hermes. Zero LLM, read-only.

Uso:
    python audit_db_storage.py            # somente diagnostico (read-only)
    python audit_db_storage.py --fix-wal  # aplica journal_size_limit=64MB + autocheckpoint

Responde, com numeros medidos:
  1. tamanho REAL em disco (db + wal + shm) -- `ls` no .db sozinho mente
  2. se o WAL esta ilimitado (journal_size_limit = -1) = causa de WAL gigante
  3. quanto VACUUM recuperaria (freelist) -- se baixo, VACUUM e perda de tempo
  4. peso dos indices FTS (normal ser ~metade do banco)
  5. se o layout v23/compacto ja esta ativo (optimize-storage nao teria efeito)
  6. distribuicao de idade das sessoes com epoch CORRETO (started_at e FLOAT)
  7. maiores tool results retidos = candidatos a cortar via tool_output.max_bytes

Detalhe e contexto: references/state-db-storage-anatomy.md
"""
import os
import sqlite3
import sys
from pathlib import Path

H = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
DB = H / "state.db"
FIX = "--fix-wal" in sys.argv


def mb(path: Path) -> float:
    return path.stat().st_size / 1024 / 1024 if path.exists() else 0.0


def main() -> int:
    if not DB.exists():
        print(f"state.db nao encontrado em {DB}")
        return 1

    print("=" * 62)
    print("1. TAMANHO REAL EM DISCO (db + wal + shm)")
    print("=" * 62)
    total = 0.0
    for name in ("state.db", "state.db-wal", "state.db-shm"):
        size = mb(H / name)
        total += size
        flag = ""
        if name.endswith("-wal") and size > mb(DB) * 0.5 and size > 100:
            flag = "  <-- WAL DESPROPORCIONAL"
        print(f"   {name:16s} {size:9.1f} MB{flag}")
    print(f"   {'TOTAL':16s} {total:9.1f} MB")
    if total > mb(DB) * 1.3:
        print(f"   AVISO: 'hermes sessions stats' reportaria so {mb(DB):.1f} MB")

    con = sqlite3.connect(str(DB), timeout=30, isolation_level=None)
    cur = con.cursor()

    print()
    print("=" * 62)
    print("2. PRAGMAS DE WAL (journal_size_limit = -1 e a causa-raiz classica)")
    print("=" * 62)
    for p in ("journal_mode", "journal_size_limit", "wal_autocheckpoint",
              "synchronous", "page_size"):
        cur.execute(f"PRAGMA {p}")
        val = cur.fetchone()[0]
        warn = "  <-- ILIMITADO: WAL nunca devolve espaco ao SO" if (
            p == "journal_size_limit" and int(val) < 0) else ""
        print(f"   {p:20s} = {val}{warn}")

    print()
    print("=" * 62)
    print("3. QUANTO O VACUUM RECUPERARIA")
    print("=" * 62)
    cur.execute("SELECT freelist_count*page_size/1024.0/1024 "
                "FROM pragma_freelist_count(), pragma_page_size()")
    free = cur.fetchone()[0]
    print(f"   freelist: {free:.1f} MB")
    print("   -> VACUUM/optimize vale a pena" if free > 20
          else "   -> VACUUM renderia pouco; nao e a alavanca")

    print()
    print("=" * 62)
    print("4. PESO DOS INDICES FTS (metade do banco e NORMAL)")
    print("=" * 62)
    fts_total = 0.0
    for t in ("messages_fts_data", "messages_fts_trigram_data", "messages_fts_cjk_data"):
        try:
            cur.execute(f"SELECT COUNT(*), SUM(LENGTH(block))/1024.0/1024 FROM {t}")
            rows, size = cur.fetchone()
            size = size or 0.0
            fts_total += size
            print(f"   {t:28s} rows={rows:>7,}  {size:8.1f} MB")
        except sqlite3.OperationalError:
            print(f"   {t:28s} (ausente)")
    print(f"   {'TOTAL FTS':28s}                {fts_total:8.1f} MB")

    print()
    print("=" * 62)
    print("5. LAYOUT v23/COMPACTO JA ATIVO? (senao, optimize-storage rende ~60%)")
    print("=" * 62)
    cur.execute("SELECT value FROM state_meta WHERE key='fts_storage_version'")
    row = cur.fetchone()
    print(f"   fts_storage_version = {row[0] if row else '(ausente)'}")
    cur.execute("SELECT sql FROM sqlite_master WHERE name='messages_fts_trigram'")
    row = cur.fetchone()
    if row and row[0]:
        external = "content='messages_fts_trigram_src'" in " ".join(row[0].split())
        print(f"   external-content no trigram: {'SIM (v23)' if external else 'NAO (legado)'}")
        if not external:
            print("   -> rodar: hermes sessions optimize-storage --yes")
    try:
        cur.execute("SELECT COUNT(*) FROM messages WHERE role<>'tool'")
        non_tool = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM messages_fts_trigram_docsize")
        tri = cur.fetchone()[0]
        print(f"   msgs non-tool={non_tool:,} vs trigram_docsize={tri:,} "
              f"({'trigram exclui tool = OK' if abs(non_tool - tri) < 100 else 'divergente'})")
    except sqlite3.OperationalError:
        pass

    print()
    print("=" * 62)
    print("6. IDADE DAS SESSOES (started_at e FLOAT epoch -- nao usar datetime())")
    print("=" * 62)
    cur.execute("SELECT MIN(started_at), MAX(started_at), COUNT(*) FROM sessions")
    lo, hi, n = cur.fetchone()
    print(f"   {n} sessoes | epoch {lo} .. {hi}")
    for days in (7, 30, 60, 90):
        cur.execute(
            "SELECT COUNT(*) FROM sessions "
            "WHERE started_at < CAST(strftime('%s','now',?) AS REAL)",
            (f"-{days} days",))
        print(f"   mais antigas que {days:3d}d: {cur.fetchone()[0]:5d} sessoes")
    print("   (se todas as janelas derem o MESMO numero, a comparacao esta errada)")

    print()
    print("=" * 62)
    print("7. MAIORES TOOL RESULTS RETIDOS (cortar via tool_output.max_bytes)")
    print("=" * 62)
    cur.execute("SELECT role, LENGTH(content) L, "
                "substr(replace(content, char(10), ' '), 1, 58) "
                "FROM messages ORDER BY L DESC LIMIT 6")
    for role, length, preview in cur.fetchall():
        print(f"   {str(role)[:9]:9s} {length:>8,} ch | {preview}")
    cur.execute("SELECT SUM(input_tokens), SUM(output_tokens) FROM sessions")
    tin, tout = cur.fetchone()
    if tin and tout:
        print(f"\n   razao input:output = {tin/tout:.0f}:1 "
              f"(in={tin:,} out={tout:,})")
        print("   -> acima de ~100:1 confirma reenvio de historico, nao geracao")

    if FIX:
        print()
        print("=" * 62)
        print("APLICANDO FIX DE WAL")
        print("=" * 62)
        cur.execute("PRAGMA journal_size_limit = 67108864")
        print(f"   journal_size_limit -> {cur.fetchone()[0]}")
        cur.execute("PRAGMA wal_autocheckpoint = 1000")
        cur.execute("PRAGMA wal_autocheckpoint")
        print(f"   wal_autocheckpoint -> {cur.fetchone()[0]}")
        cur.execute("PRAGMA wal_checkpoint(PASSIVE)")
        busy, log, ckpt = cur.fetchone()
        print(f"   checkpoint: busy={busy} log={log} ckpt={ckpt} "
              f"| WAL={mb(H/'state.db-wal'):.1f} MB")
        if log != ckpt:
            print("   NOTA: checkpoint travado -> o gateway mantem snapshot de leitura.")
            print("   Truncar exige `hermes gateway restart` num shell EXTERNO ao gateway")
            print("   (bloqueado de dentro). NUNCA taskkill /F -- corrompe o state.db.")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
