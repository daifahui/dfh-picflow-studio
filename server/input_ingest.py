"""输入摄取：文件夹扫描 / zip·7z 解压、自然排序、图片过滤。"""
from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path

from .state import Item

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

_nat = re.compile(r"(\d+)")


def natural_key(s: str) -> list:
    return [int(p) if p.isdigit() else p.lower() for p in _nat.split(s)]


def is_image(p: Path) -> bool:
    return p.suffix.lower() in IMAGE_EXTS and p.is_file()


def scan_dir(root: Path) -> list[Path]:
    """root 下（含一层子目录）的全部图片，自然排序。"""
    files = [p for p in root.rglob("*") if is_image(p)]
    files.sort(key=lambda p: natural_key(str(p.relative_to(root))))
    return files


def extract_archive(archive: Path, dest: Path) -> list[Path]:
    """解压 zip/7z 到 dest，返回排序后的图片列表。"""
    dest.mkdir(parents=True, exist_ok=True)
    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as zf:
            for m in zf.infolist():
                if m.is_dir():
                    continue
                # 跳过 mac 垃圾目录
                if "__MACOSX" in m.filename:
                    continue
                target = dest / m.filename
                if not str(target.resolve()).startswith(str(dest.resolve())):
                    continue  # 防 zip slip
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(m) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
    elif archive.suffix.lower() == ".7z":
        import py7zr
        with py7zr.SevenZipFile(archive) as zf:
            zf.extractall(dest)
    else:
        raise ValueError(f"不支持的压缩包格式：{archive.suffix}（支持 zip / 7z）")
    return scan_dir(dest)


def build_items(files: list[Path], source_dir: Path, keep_in_place: bool,
                task_source_dir: Path) -> tuple[list[Item], Path]:
    """生成条目列表。

    keep_in_place=True（文件夹输入）：文件留在原地，source_root 为其父目录；
    否则（压缩包）：已解压到 task_source_dir，重命名为 0001_原名.ext。
    返回 (items, 允许访问的根目录)。
    """
    items: list[Item] = []
    if keep_in_place:
        for i, p in enumerate(files, 1):
            items.append(Item(seq=i, src_filename=p.name, src_path=str(p)))
        return items, files[0].parent if files else source_dir

    task_source_dir.mkdir(parents=True, exist_ok=True)
    for i, p in enumerate(files, 1):
        canon = f"{i:04d}_{p.name}"
        target = task_source_dir / canon
        if p.resolve() != target.resolve():
            shutil.copy2(p, target)
        items.append(Item(seq=i, src_filename=canon, src_path=str(target)))
    return items, task_source_dir
