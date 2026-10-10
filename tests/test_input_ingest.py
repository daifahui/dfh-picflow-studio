"""input_ingest 单元测试：自然排序、图片过滤、zip 解压、条目构建。"""
from pathlib import Path

from server import input_ingest


def test_natural_key_orders_numbers_numerically():
    files = ["img2.png", "img10.png", "img1.png", "img100.png", "img20.png"]
    assert sorted(files, key=input_ingest.natural_key) == \
        ["img1.png", "img2.png", "img10.png", "img20.png", "img100.png"]


def test_natural_key_mixed_case_and_text():
    files = ["B.png", "a.png", "第10图.png", "第2图.png"]
    assert sorted(files, key=input_ingest.natural_key) == \
        ["a.png", "B.png", "第2图.png", "第10图.png"]


def test_scan_dir_sorts_and_filters(image_dir: Path):
    (image_dir / "not_image.txt").write_text("x")
    files = input_ingest.scan_dir(image_dir)
    assert [p.name for p in files] == ["a1.webp", "a2.jpg", "b10.png"]


def test_scan_dir_includes_subdirs(image_dir: Path):
    sub = image_dir / "sub"
    sub.mkdir()
    (sub / "z.png").write_bytes(image_dir.joinpath("a1.webp").read_bytes())
    names = [p.name for p in input_ingest.scan_dir(image_dir)]
    assert "z.png" in names


def test_extract_zip_skips_macos_and_filters(tmp_path: Path, zip_archive: Path):
    # 重新造一个含 __MACOSX 与 txt 的包
    import zipfile
    zp = tmp_path / "with_junk.zip"
    png = next(Path(tmp_path / "imgs").glob("*.png")).read_bytes()
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("a.png", png)
        zf.writestr("__MACOSX/a.png", b"junk")
        zf.writestr("notes.txt", b"text")
        zf.writestr("sub/b.png", png)
    dest = tmp_path / "out"
    files = input_ingest.extract_archive(zp, dest)
    names = {p.name for p in files}
    assert names == {"a.png", "b.png"}          # txt 与 __MACOSX 均被排除
    assert (dest / "sub" / "b.png").exists()


def test_extract_unsupported_suffix_raises(tmp_path: Path):
    import pytest
    bad = tmp_path / "x.rar"
    bad.write_bytes(b"whatever")
    with pytest.raises(ValueError, match="zip / 7z"):
        input_ingest.extract_archive(bad, tmp_path / "o")


def test_build_items_keeps_in_place(image_dir: Path):
    files = input_ingest.scan_dir(image_dir)
    items, root = input_ingest.build_items(files, image_dir,
                                           keep_in_place=True,
                                           task_source_dir=image_dir / "x")
    assert [i.seq for i in items] == [1, 2, 3]
    assert items[0].src_filename == "a1.webp"
    assert Path(items[0].src_path).read_bytes()      # 原文件未被移动
    assert root == files[0].parent


def test_build_items_archive_copies_and_renames(image_dir: Path, tmp_path: Path):
    files = input_ingest.scan_dir(image_dir)
    target = tmp_path / "task" / "source"
    items, root = input_ingest.build_items(files, image_dir,
                                           keep_in_place=False,
                                           task_source_dir=target)
    assert items[1].src_filename == "0002_a2.jpg"
    assert (target / "0001_a1.webp").exists()
    assert root == target                             # 访问边界收敛到任务目录
