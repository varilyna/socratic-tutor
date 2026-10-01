import io
import re
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


MIN_CHARS = 150
MAX_CHARS = 14000


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def validate_material(text: str) -> str:
    text = text.strip()
    if len(text) < MIN_CHARS:
        raise ValueError(f"Добавьте учебный текст длиной от {MIN_CHARS} символов.")
    if len(text) > MAX_CHARS:
        raise ValueError(f"Для демо выберите фрагмент до {MAX_CHARS} символов.")
    return text


def read_upload(data: bytes, filename: str) -> str:
    if len(data) > 2_000_000:
        raise ValueError("Файл должен быть меньше 2 МБ.")
    if filename.lower().endswith(".docx"):
        try:
            with ZipFile(io.BytesIO(data)) as archive:
                info = archive.getinfo("word/document.xml")
                if info.file_size > 5_000_000:
                    raise ValueError("Распакованный документ слишком большой.")
                root = ElementTree.fromstring(archive.read(info))
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            return "\n".join("".join(t.text or "" for t in p.findall(".//w:t", ns))
                             for p in root.findall(".//w:p", ns))
        except (BadZipFile, KeyError, ElementTree.ParseError, OSError) as exc:
            raise ValueError("Не удалось прочитать DOCX. Вставьте текст вручную.") from exc
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Текстовый файл должен быть в кодировке UTF-8.") from exc
