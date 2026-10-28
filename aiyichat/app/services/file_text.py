"""入站文件的可解析文本提取。

仅使用标准库，避免为渠道附件引入新的运行时依赖；不支持的格式保留文件本体，
由上层向模型提供明确的“已保存但不可解析”提示。
"""

import csv
import io
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree

MAX_EXTRACT_CHARS = 20000
MAX_XML_BYTES = 20 * 1024 * 1024
_TEXT_SUFFIXES = frozenset({
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log",
    ".yaml", ".yml", ".xml", ".html", ".htm", ".py", ".js", ".ts",
    ".css", ".sql",
})


def _read_zip_xml(archive: zipfile.ZipFile, name: str) -> ElementTree.Element:
    """先按声明的解压体积拒绝压缩炸弹，再禁用 DTD/ENTITY 后解析。"""
    info = archive.getinfo(name)
    if info.file_size > MAX_XML_BYTES:
        raise ValueError("XML 内容过大")
    raw = archive.read(name)
    if len(raw) > MAX_XML_BYTES:
        raise ValueError("XML 内容过大")
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("XML 包含不允许的 DTD/ENTITY")
    return ElementTree.fromstring(raw)


def _extract_docx(data: bytes) -> str:
    """从 docx 的 word/document.xml 提取段落文本。"""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        root = _read_zip_xml(archive, "word/document.xml")
    texts = [node.text or "" for node in root.iter() if node.tag.endswith("}t")]
    return "\n".join(part for part in texts if part.strip())


def _extract_xlsx(data: bytes) -> str:
    """从 xlsx 共享字符串与工作表 XML 提取单元格文本。"""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = _read_zip_xml(archive, "xl/sharedStrings.xml")
            for item in root:
                shared.append(
                    "".join(
                        node.text or ""
                        for node in item.iter()
                        if node.tag.endswith("}t")
                    )
                )
        rows: list[str] = []
        for name in sorted(n for n in names if n.startswith("xl/worksheets/sheet")):
            root = _read_zip_xml(archive, name)
            for row in root.iter():
                if not row.tag.endswith("}row"):
                    continue
                cells: list[str] = []
                for cell in row:
                    if not cell.tag.endswith("}c"):
                        continue
                    value_node = next(
                        (n for n in cell if n.tag.endswith("}v")), None
                    )
                    if value_node is None or value_node.text is None:
                        continue
                    value = value_node.text
                    if cell.attrib.get("t") == "s":
                        try:
                            value = shared[int(value)]
                        except (ValueError, IndexError):
                            pass
                    cells.append(value)
                if cells:
                    rows.append("\t".join(cells))
        return "\n".join(rows)


def extract_text_from_file(data: bytes, filename: str) -> str:
    """提取可解析文件正文；未知格式或损坏文件返回空串。"""
    suffix = Path(filename).suffix.lower()
    try:
        if suffix in _TEXT_SUFFIXES:
            text = data.decode("utf-8", errors="replace")
        elif suffix == ".docx":
            text = _extract_docx(data)
        elif suffix == ".xlsx":
            text = _extract_xlsx(data)
        elif suffix == ".csv":
            text = "\n".join(
                "\t".join(row)
                for row in csv.reader(io.StringIO(
                    data.decode("utf-8", errors="replace")
                ))
            )
        elif suffix in (".jsonl", ".ndjson"):
            lines = data.decode("utf-8", errors="replace").splitlines()
            text = "\n".join(
                json.dumps(json.loads(line), ensure_ascii=False)
                for line in lines if line.strip()
            )
        else:
            return ""
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ElementTree.ParseError):
        return ""
    return text.strip()[:MAX_EXTRACT_CHARS]
