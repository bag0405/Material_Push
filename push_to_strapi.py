"""
Заливает материалы из materials.json в Strapi через REST API.

ПОДГОТОВКА:
1. В админке Strapi зайди в Settings -> API Tokens -> Create new API Token.
   Тип токена: "Full access" (или как минимум Create/Find для Material).
2. Скопируй токен и подставь его в переменную окружения STRAPI_TOKEN,
   не хранить токен прямо в коде — так безопаснее:
       export STRAPI_TOKEN="вставь_сюда_токен"      (Linux/macOS)
       $env:STRAPI_TOKEN="вставь_сюда_токен"        (Windows PowerShell)
3. Проверь STRAPI_URL ниже — это твой домен CMS.

ЗАПУСК:
    python push_to_strapi.py

Скрипт:
- пропускает материалы, у которых title уже существует в базе (чтобы не было дублей
  при повторном запуске),
- логирует, что получилось, а что нет,
- делает паузу между запросами, чтобы не заваливать сервер.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse


def to_idna_url(url: str) -> str:
    """
    Кириллические домены (например, баумантех.дети) нужно закодировать в punycode
    (xn--...), иначе HTTP-библиотеки Python падают на непонятном заголовке Host.
    Эта функция делает это автоматически, независимо от того, как задан STRAPI_URL —
    кириллицей или уже в punycode.
    """
    parts = urllib.parse.urlsplit(url)
    hostname = parts.hostname or ""
    try:
        idna_host = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        idna_host = hostname  # уже ascii или punycode

    netloc = idna_host
    if parts.port:
        netloc += f":{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


STRAPI_URL = to_idna_url(
    os.environ.get("STRAPI_URL", "https://cms.баумантех.дети").rstrip("/")
)
STRAPI_TOKEN = os.environ.get("STRAPI_TOKEN")
MATERIALS_JSON = "materials.json"
DELAY_SECONDS = 0.3  # пауза между запросами

# Значение material_type, которое должно совпадать с опцией в дропдауне Strapi
DEFAULT_MATERIAL_TYPE = "link"

# Название блока, к которому нужно привязать все создаваемые материалы.
# Можно переопределить переменной окружения BLOCK_TITLE, если понадобится
# заливать материалы в другой блок без правки кода.
BLOCK_TITLE = os.environ.get("BLOCK_TITLE", "Пентест. Защита сетей")


def strapi_request(method, path, payload=None):
    url = f"{STRAPI_URL}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if STRAPI_TOKEN:
        req.add_header("Authorization", f"Bearer {STRAPI_TOKEN}")
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read()
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body.decode("utf-8", errors="replace")}
        return e.code, parsed


def fetch_existing_titles():
    """Собирает все уже существующие title, постранично, чтобы не плодить дубли."""
    titles = set()
    page = 1
    page_size = 100
    while True:
        status, resp = strapi_request(
            "GET",
            f"/api/materials?pagination[page]={page}&pagination[pageSize]={page_size}&fields[0]=title",
        )
        if status != 200:
            print(f" Не удалось получить существующие материалы (status={status}): {resp}")
            break
        items = resp.get("data", [])
        if not items:
            break
        for item in items:
            attrs = item.get("attributes", item)  # на случай разных версий Strapi
            title = attrs.get("title")
            if title:
                titles.add(title)
        meta = resp.get("meta", {}).get("pagination", {})
        if page >= meta.get("pageCount", page):
            break
        page += 1
    return titles


def fetch_block_id(title):
    """
    Находит id блока (Collection Type Block) по названию.
    Не привязывается к конкретному имени поля (title/name/heading и т.п.) —
    просто забирает все блоки и ищет среди их полей значение, совпадающее с title.
    """
    status, resp = strapi_request(
        "GET",
        "/api/blocks?pagination[pageSize]=100",
    )
    if status != 200:
        print(f" Не удалось получить список блоков (status={status}): {resp}")
        return None

    items = resp.get("data", [])
    if not items:
        print(" В Strapi нет ни одного блока.")
        return None

    for item in items:
        attrs = item.get("attributes", item)  # на случай разных версий Strapi
        for value in attrs.values():
            if isinstance(value, str) and value.strip() == title.strip():
                block_id = item.get("id")
                print(f"Найден блок '{title}' — id={block_id}")
                return block_id

    available = []
    for item in items:
        attrs = item.get("attributes", item)
        for value in attrs.values():
            if isinstance(value, str) and value.strip():
                available.append(value.strip())
                break
    print(f" Блок с названием '{title}' не найден. Доступные блоки: {available}")
    return None


def fetch_all_materials_with_block():
    """
    Возвращает список всех материалов вида {id, title, block_id}.
    block_id = None, если relation ещё не проставлена.
    """
    result = []
    page = 1
    page_size = 100
    while True:
        status, resp = strapi_request(
            "GET",
            f"/api/materials?pagination[page]={page}&pagination[pageSize]={page_size}"
            f"&populate=block&fields[0]=title",
        )
        if status != 200:
            print(f" Не удалось получить материалы для проверки block (status={status}): {resp}")
            break
        items = resp.get("data", [])
        if not items:
            break
        for item in items:
            attrs = item.get("attributes", item)
            block_rel = attrs.get("block")
            block_id = None
            if isinstance(block_rel, dict):
                block_data = block_rel.get("data")
                if isinstance(block_data, dict):
                    block_id = block_data.get("id")
                elif isinstance(block_data, list) and block_data:
                    block_id = block_data[0].get("id")
            result.append({
                "id": item.get("id"),
                "title": attrs.get("title"),
                "block_id": block_id,
            })
        meta = resp.get("meta", {}).get("pagination", {})
        if page >= meta.get("pageCount", page):
            break
        page += 1
    return result


def update_material_block(material_id, block_id):
    payload = {"data": {"block": block_id}}
    return strapi_request("PATCH", f"/api/materials/{material_id}", payload)


def create_material(entry, block_id=None):
    data = {
        "title": entry["title"],
        "description": entry["description"],
        "material_type": entry.get("material_type", DEFAULT_MATERIAL_TYPE),
        "external_url": entry["external_url"],
    }
    if block_id is not None:
        data["block"] = block_id

    payload = {"data": data}
    return strapi_request("POST", "/api/materials", payload)


def main():
    if not STRAPI_TOKEN:
        print(" Не задан STRAPI_TOKEN. Смотри инструкцию в шапке файла.")
        sys.exit(1)

    with open(MATERIALS_JSON, encoding="utf-8") as f:
        materials = json.load(f)

    print(f"Загружено {len(materials)} материалов из {MATERIALS_JSON}")

    print(f"Ищу блок '{BLOCK_TITLE}'...")
    block_id = fetch_block_id(BLOCK_TITLE)
    if block_id is None:
        print("Продолжаю без привязки к блоку (все материалы будут созданы без relation 'block').")

    print("Проверяю уже существующие записи в Strapi...")
    existing_titles = fetch_existing_titles()
    print(f"Уже в базе: {len(existing_titles)} записей")

    created, skipped, failed = 0, 0, 0

    for i, entry in enumerate(materials, 1):
        if entry["title"] in existing_titles:
            skipped += 1
            continue

        status, resp = create_material(entry, block_id=block_id)
        if status in (200, 201):
            created += 1
            existing_titles.add(entry["title"])
            print(f"[{i}/{len(materials)}] {entry['title']}")
        else:
            failed += 1
            print(f"[{i}/{len(materials)}]  {entry['title']} — status={status} — {resp}")

        time.sleep(DELAY_SECONDS)

    print("\n--- ИТОГО ---")
    print(f"Создано: {created}")
    print(f"Пропущено (уже были): {skipped}")
    print(f"Ошибок: {failed}")




if __name__ == "__main__":
    main()
