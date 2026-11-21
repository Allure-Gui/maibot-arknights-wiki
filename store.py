"""独立轻量 SQLite 持久化缓存与立绘 LRU 管理。

所有运行期数据严格隔离在插件专属数据目录，绝不读写 MaiBot 核心数据库。
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional


class WikiCacheStore:
    """PRTS 页面文本、重定向映射与立绘缩图本地缓存仓库。"""

    def __init__(self, data_dir: Path, image_max_mb: int = 200) -> None:
        self.data_dir = Path(data_dir)
        self.image_dir = self.data_dir / "images"
        self.db_path = self.data_dir / "arknights_cache.db"
        self.image_max_bytes = max(20, int(image_max_mb)) * 1024 * 1024
        self._lock = threading.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pages (
                    title TEXT PRIMARY KEY,
                    wikitext TEXT NOT NULL,
                    is_operator INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS redirects (
                    alias TEXT PRIMARY KEY,
                    target TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS images (
                    cache_key TEXT PRIMARY KEY,
                    file_name TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    source_url TEXT NOT NULL,
                    last_used_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS image_lists (
                    operator_name TEXT PRIMARY KEY,
                    files_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    def get_page(self, title: str, ttl_seconds: float) -> Optional[tuple[str, bool]]:
        """读取未过期的页面 Wikitext 缓存，命中返回 (wikitext, is_operator)。"""
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT wikitext, is_operator, updated_at FROM pages WHERE title = ?",
                (title,),
            ).fetchone()
        if not row:
            return None
        wikitext, is_operator, updated_at = row
        if ttl_seconds > 0 and (now - float(updated_at)) > ttl_seconds:
            return None
        return str(wikitext), bool(is_operator)

    def put_page(self, title: str, wikitext: str, is_operator: bool) -> None:
        """写入或刷新页面 Wikitext 缓存。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pages(title, wikitext, is_operator, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(title) DO UPDATE SET
                    wikitext = excluded.wikitext,
                    is_operator = excluded.is_operator,
                    updated_at = excluded.updated_at
                """,
                (title, wikitext, 1 if is_operator else 0, time.time()),
            )

    def get_redirect(self, alias: str, ttl_seconds: float = 86400 * 14) -> Optional[str]:
        """读取 PRTS 重定向缓存。"""
        key = alias.strip().lower()
        if not key:
            return None
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT target, updated_at FROM redirects WHERE alias = ?",
                (key,),
            ).fetchone()
        if not row:
            return None
        target, updated_at = row
        if ttl_seconds > 0 and (now - float(updated_at)) > ttl_seconds:
            return None
        return str(target)

    def put_redirect(self, alias: str, target: str) -> None:
        """记录别名到标准 PRTS 词条名的映射。"""
        key = alias.strip().lower()
        if not key or not target.strip():
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO redirects(alias, target, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(alias) DO UPDATE SET
                    target = excluded.target,
                    updated_at = excluded.updated_at
                """,
                (key, target.strip(), time.time()),
            )

    def get_image_list(self, operator_name: str, ttl_seconds: float) -> Optional[str]:
        """读取干员立绘文件名列表 JSON 缓存。"""
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT files_json, updated_at FROM image_lists WHERE operator_name = ?",
                (operator_name,),
            ).fetchone()
        if not row:
            return None
        files_json, updated_at = row
        if ttl_seconds > 0 and (now - float(updated_at)) > ttl_seconds:
            return None
        return str(files_json)

    def put_image_list(self, operator_name: str, files_json: str) -> None:
        """保存干员立绘文件名列表 JSON 缓存。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO image_lists(operator_name, files_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(operator_name) DO UPDATE SET
                    files_json = excluded.files_json,
                    updated_at = excluded.updated_at
                """,
                (operator_name, files_json, time.time()),
            )

    def get_image_bytes(self, cache_key: str) -> Optional[bytes]:
        """读取本地缓存的立绘图片字节，并更新 LRU 访问时间。"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT file_name FROM images WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if not row:
                return None
            file_path = self.image_dir / str(row[0])
            if not file_path.is_file():
                conn.execute("DELETE FROM images WHERE cache_key = ?", (cache_key,))
                return None
            conn.execute(
                "UPDATE images SET last_used_at = ? WHERE cache_key = ?",
                (time.time(), cache_key),
            )
        try:
            return file_path.read_bytes()
        except OSError:
            return None

    def put_image_bytes(self, cache_key: str, data: bytes, source_url: str, ext: str = ".png") -> Path:
        """保存立绘字节到本地磁盘，并在超限时执行 LRU 淘汰。"""
        safe_ext = ext if ext.startswith(".") else f".{ext}"
        digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:24]
        file_name = f"{digest}{safe_ext}"
        file_path = self.image_dir / file_name
        tmp_path = self.image_dir / f"{file_name}.tmp"
        tmp_path.write_bytes(data)
        tmp_path.replace(file_path)

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO images(cache_key, file_name, size_bytes, source_url, last_used_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    file_name = excluded.file_name,
                    size_bytes = excluded.size_bytes,
                    source_url = excluded.source_url,
                    last_used_at = excluded.last_used_at
                """,
                (cache_key, file_name, len(data), source_url, time.time()),
            )
            self._evict_images_locked(conn)
        return file_path

    def _evict_images_locked(self, conn: sqlite3.Connection) -> None:
        """按 LRU 淘汰最久未使用的立绘缓存，防止磁盘膨胀。"""
        total_row = conn.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM images").fetchone()
        total = int(total_row[0] or 0) if total_row else 0
        if total <= self.image_max_bytes:
            return
        target_bytes = int(self.image_max_bytes * 0.85)
        rows = conn.execute(
            "SELECT cache_key, file_name, size_bytes FROM images ORDER BY last_used_at ASC"
        ).fetchall()
        for cache_key, file_name, size_bytes in rows:
            if total <= target_bytes:
                break
            try:
                (self.image_dir / str(file_name)).unlink(missing_ok=True)
            except OSError:
                pass
            conn.execute("DELETE FROM images WHERE cache_key = ?", (cache_key,))
            total -= int(size_bytes or 0)

    def clear_all(self) -> tuple[int, int]:
        """清空全部文本与图片缓存，返回 (清除页面数, 清除图片数)。"""
        with self._lock, self._connect() as conn:
            p_cnt = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            i_cnt = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
            conn.executescript("DELETE FROM pages; DELETE FROM redirects; DELETE FROM images; DELETE FROM image_lists;")
        for fp in self.image_dir.glob("*"):
            try:
                if fp.is_file():
                    fp.unlink()
            except OSError:
                pass
        return int(p_cnt), int(i_cnt)
