"""Terminal-based personal AI agent for Ubuntu."""

import ast
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import psutil

from core.config import ConfigurationError, load_config
from core.llm_client import (
    ChatMessage,
    LLMClient,
    LLMError,
    OllamaClient,
    ToolDefinition,
)

MODEL = "gemma4:e2b"
MAX_STEPS = 8
MAX_READ_CHARS = 6000
MAX_SEARCH_RESULTS = 20
MAX_CALCULATION_LENGTH = 300
MAX_CALCULATION_DEPTH = 30
MAX_INTEGER_BITS = 4096
WEATHER_CODES = {
    0: "Ochiq",
    1: "Asosan ochiq",
    2: "Qisman bulutli",
    3: "Bulutli",
    45: "Tuman",
    48: "Qirovli tuman",
    51: "Yengil shivalama",
    53: "Shivalama",
    55: "Kuchli shivalama",
    56: "Muzli yengil shivalama",
    57: "Muzli shivalama",
    61: "Yengil yomg'ir",
    63: "Yomg'ir",
    65: "Kuchli yomg'ir",
    66: "Muzli yengil yomg'ir",
    67: "Muzli kuchli yomg'ir",
    71: "Yengil qor",
    73: "Qor",
    75: "Kuchli qor",
    77: "Muz donachalari",
    80: "Yengil jala",
    81: "Jala",
    82: "Kuchli jala",
    85: "Yengil qor jala",
    86: "Kuchli qor jala",
    95: "Momaqaldiroqli chaqmoq",
    96: "Do'l aralash momaqaldiroq",
    99: "Kuchli do'lli momaqaldiroq",
}
SYSTEM_PROMPT = """Siz terminalda ishlaydigan shaxsiy AI agentsiz. Foydalanuvchi bilan faqat o'zbek tilida (lotin yozuvida) gaplashing.
Kerak bo'lsa, faqat quyida berilgan vositalardan foydalaning; model interfeysi vositalarni native tool call yoki tekshiriladigan {"tool":"nomi","args":{...}} JSON shaklida qabul qiladi. Oddiy yakuniy javobni tabiiy o'zbek tilida bering.
Natijaga asoslanib ishlang; kerakli ma'lumot yetishmasa vositani qayta chaqiring yoki aniq cheklovni ayting.
Vositalar:
- find_files: fayllarni nom bo'lagi yoki kengaytma bo'yicha izlash; query, ext, folder argumentlari ixtiyoriy.
- read_text: .txt yoki .md faylini o'qish; path talab qilinadi.
- save_note: eslatmani saqlash; title va content talab qilinadi, saqlashdan oldin tasdiq so'raladi.
- calculate: arifmetik ifodani hisoblash; expression talab qilinadi.
- system_status: CPU, RAM va disk holati.
- get_weather: shahar ob-havosi; city talab qilinadi."""

TOOL_DEFINITIONS = (
    ToolDefinition(
        name="find_files",
        description="Nom bo'lagi yoki kengaytma bilan fayllarni qidirish.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "ext": {"type": "string"},
                "folder": {"type": "string"},
            },
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="read_text",
        description="UTF-8 .txt yoki .md faylning boshidan 6000 belgigacha o'qish.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "minLength": 1}},
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="save_note",
        description="Foydalanuvchi tasdig'idan keyin agent_notes ichiga eslatma yozish.",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 1},
                "content": {"type": "string"},
            },
            "required": ["title", "content"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="calculate",
        description="Xavfsiz ruxsat etilgan arifmetik ifodani hisoblash.",
        parameters={
            "type": "object",
            "properties": {"expression": {"type": "string", "minLength": 1}},
            "required": ["expression"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="system_status",
        description="CPU, RAM va root disk holatini qaytarish.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolDefinition(
        name="get_weather",
        description="Open-Meteo orqali shaharning joriy ob-havosini olish.",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string", "minLength": 1}},
            "required": ["city"],
            "additionalProperties": False,
        },
    ),
)


def find_files(query: str = "", ext: str = "", folder: str = ".") -> str:
    """Papka ichidan nom bo'lagi yoki kengaytma bo'yicha ko'pi bilan 20 fayl topadi."""
    root = Path(folder).expanduser()
    if not root.is_dir():
        return f"XATO: Papka topilmadi yoki papka emas: {root}"

    needle = query.casefold().strip()
    suffix = ext.casefold().strip()
    if suffix and not suffix.startswith("."):
        suffix = f".{suffix}"
    if not needle and not suffix:
        return "XATO: Qidirish uchun query yoki ext qiymatini kiriting."

    matches: list[str] = []
    try:
        for current, dirs, filenames in os.walk(root):
            dirs[:] = [name for name in dirs if not name.startswith(".")]
            for filename in filenames:
                path = Path(current) / filename
                if (needle and needle in filename.casefold()) or (
                    suffix and path.suffix.casefold() == suffix
                ):
                    matches.append(str(path))
                    if len(matches) >= MAX_SEARCH_RESULTS:
                        return "Topilgan fayllar:\n" + "\n".join(matches)
    except OSError as exc:
        return f"XATO: Fayllarni qidirib bo'lmadi: {exc}"

    if not matches:
        return "Mos fayl topilmadi."
    return "Topilgan fayllar:\n" + "\n".join(matches)


def read_text(path: str) -> str:
    """UTF-8 formatidagi .txt yoki .md faylning boshidan 6000 belgigacha o'qiydi."""
    file_path = Path(path).expanduser()
    if file_path.suffix.casefold() not in {".txt", ".md"}:
        return "XATO: Faqat .txt va .md fayllarni o'qish mumkin."
    if not file_path.is_file():
        return f"XATO: Fayl topilmadi: {file_path}"
    try:
        with file_path.open("r", encoding="utf-8") as file:
            content = file.read(MAX_READ_CHARS + 1)
    except (OSError, UnicodeError) as exc:
        return f"XATO: Faylni o'qib bo'lmadi: {exc}"

    if len(content) > MAX_READ_CHARS:
        return content[:MAX_READ_CHARS] + "\n\n[Matn 6000 belgida qisqartirildi.]"
    return content


def save_note(title: str, content: str) -> str:
    """Foydalanuvchi tasdig'idan keyin matnni ~/agent_notes/ ichiga saqlaydi."""
    safe_title = "".join(
        char.lower() if char.isascii() and char.isalnum() else "-"
        for char in title.strip()
    )
    safe_title = "-".join(part for part in safe_title.split("-") if part) or "eslatma"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = Path.home() / "agent_notes" / f"{timestamp}-{safe_title}.md"

    print(f"Saqlanadigan fayl: {target}")
    print("---")
    print(content)
    print("---")
    confirmation = input("Saqlaymizmi? (ha/yo'q) ").strip().casefold()
    if confirmation != "ha":
        return "Foydalanuvchi saqlashni rad etdi; fayl yozilmadi."

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Eslatma saqlandi: {target}"


def _evaluate_node(node: ast.AST, depth: int = 0) -> int | float:
    """Ruxsat etilgan arifmetik AST tugunini xavfsiz hisoblaydi."""
    if depth > MAX_CALCULATION_DEPTH:
        raise ValueError("Ifoda juda murakkab.")
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        if isinstance(node.value, bool):
            raise ValueError("Mantiqiy qiymatlar qabul qilinmaydi.")
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _evaluate_node(node.operand, depth + 1)
        result = value if isinstance(node.op, ast.UAdd) else -value
    elif isinstance(node, ast.BinOp):
        left = _evaluate_node(node.left, depth + 1)
        right = _evaluate_node(node.right, depth + 1)
        if isinstance(node.op, ast.Add):
            result = left + right
        elif isinstance(node.op, ast.Sub):
            result = left - right
        elif isinstance(node.op, ast.Mult):
            result = left * right
        elif isinstance(node.op, ast.Div):
            result = left / right
        elif isinstance(node.op, ast.FloorDiv):
            result = left // right
        elif isinstance(node.op, ast.Mod):
            result = left % right
        elif isinstance(node.op, ast.Pow):
            if abs(right) > 100:
                raise ValueError("Daraja qiymati -100 va 100 oralig'ida bo'lishi kerak.")
            result = left**right
        else:
            raise ValueError("Bu amal qo'llab-quvvatlanmaydi.")
    else:
        raise ValueError("Faqat sonlar, +, -, *, /, //, %, ** va qavslar ruxsat etiladi.")

    if isinstance(result, int) and result.bit_length() > MAX_INTEGER_BITS:
        raise ValueError("Hisob natijasi ruxsat etilgan hajmdan katta.")
    return result


def calculate(expression: str) -> str:
    """Ruxsat etilgan arifmetik amallarni ast orqali eval ishlatmasdan hisoblaydi."""
    if len(expression) > MAX_CALCULATION_LENGTH:
        return "XATO: Hisoblash ifodasi juda uzun."
    try:
        parsed = ast.parse(expression, mode="eval")
        result = _evaluate_node(parsed.body)
    except ZeroDivisionError:
        return "XATO: Nolga bo'lish mumkin emas."
    except (SyntaxError, ValueError, OverflowError) as exc:
        return f"XATO: Hisoblash ifodasi noto'g'ri: {exc}"
    return f"Natija: {result}"


def system_status() -> str:
    """CPU, RAM va / disk holatini GB va foizlarda qaytaradi."""
    cpu_percent = psutil.cpu_percent(interval=0.2)
    core_count = psutil.cpu_count(logical=True) or 0
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    gib = 1024**3
    return (
        f"CPU: {cpu_percent:.1f}%; mantiqiy yadro: {core_count}\n"
        f"RAM: jami {memory.total / gib:.1f} GB, "
        f"bo'sh {memory.available / gib:.1f} GB, band {memory.percent:.1f}%\n"
        f"Disk (/): jami {disk.total / gib:.1f} GB, "
        f"bo'sh {disk.free / gib:.1f} GB, band {disk.percent:.1f}%"
    )


def get_weather(city: str) -> str:
    """Open-Meteo orqali shahar koordinatalari va joriy ob-havoni oladi."""
    if not city.strip():
        return "XATO: Shahar nomini kiriting."
    try:
        geocoding_url = (
            "https://geocoding-api.open-meteo.com/v1/search?"
            + urlencode({"name": city, "count": 1, "language": "uz", "format": "json"})
        )
        with urlopen(geocoding_url, timeout=10) as response:
            locations = json.loads(response.read().decode("utf-8")).get("results", [])
        if not locations:
            return f"XATO: '{city}' shahri topilmadi."

        location = locations[0]
        latitude = location["latitude"]
        longitude = location["longitude"]
        forecast_url = (
            "https://api.open-meteo.com/v1/forecast?"
            + urlencode(
                {
                    "latitude": latitude,
                    "longitude": longitude,
                    "current": (
                        "temperature_2m,apparent_temperature,relative_humidity_2m,"
                        "wind_speed_10m,precipitation,weather_code"
                    ),
                }
            )
        )
        with urlopen(forecast_url, timeout=10) as response:
            current = json.loads(response.read().decode("utf-8"))["current"]
    except URLError as exc:
        return f"XATO: Ob-havo xizmatiga ulanib bo'lmadi: {exc}"
    except (TimeoutError, KeyError, IndexError, TypeError, ValueError) as exc:
        return f"XATO: Ob-havo ma'lumotini olishda xatolik: {exc}"

    code = current.get("weather_code")
    description = WEATHER_CODES.get(code, f"Noma'lum ob-havo kodi ({code})")
    city_name = location.get("name", city)
    country = location.get("country")
    place = f"{city_name}, {country}" if country else city_name
    return (
        f"{place}: {description}. Harorat {current.get('temperature_2m')} °C, "
        f"sezilishi {current.get('apparent_temperature')} °C, "
        f"namlik {current.get('relative_humidity_2m')}%, "
        f"shamol {current.get('wind_speed_10m')} km/soat, "
        f"yog'ingarchilik {current.get('precipitation')} mm."
    )


TOOLS = {
    "find_files": find_files,
    "read_text": read_text,
    "save_note": save_note,
    "calculate": calculate,
    "system_status": system_status,
    "get_weather": get_weather,
}


def _show_preview(result: str) -> None:
    """Vosita natijasining terminalda ko'rinadigan qisqa nusxasini chiqaradi."""
    preview = " ".join(result.split())
    if len(preview) > 220:
        preview = preview[:217] + "..."
    print(f"[natija] {preview}")


def run_task(
    task: str,
    *,
    model: str = MODEL,
    client: LLMClient | None = None,
) -> None:
    """Bitta foydalanuvchi vazifasini 8 tagacha vosita qadami bilan bajaradi."""
    llm_client = client if client is not None else OllamaClient(model=model)
    history = [ChatMessage(role="user", content=task)]
    steps = 0

    while True:
        try:
            response = llm_client.chat(
                history,
                system_prompt=SYSTEM_PROMPT,
                tools=TOOL_DEFINITIONS,
            )
        except LLMError as exc:
            print(
                f"XATO: Ollama so'rovi bajarilmadi ({type(exc).__name__}): {exc}. "
                f"`ollama serve` va `{model}` modeli mavjudligini tekshiring."
            )
            return

        if not response.tool_calls:
            print(f"Agent: {response.content}")
            return

        if len(response.tool_calls) > MAX_STEPS - steps:
            print(
                f"Vazifa {MAX_STEPS} vosita qadamidan oshdi. "
                "Iltimos, vazifani kichikroq qismlarga ajrating."
            )
            return

        history.append(response.assistant_message)
        native_tool_calls = response.assistant_message.tool_calls is not None
        for tool_call in response.tool_calls:
            steps += 1
            tool_name = tool_call.name
            args = tool_call.arguments
            print(
                f"[step] Vosita: {tool_name} "
                f"args={json.dumps(args, ensure_ascii=False, sort_keys=True)}"
            )
            try:
                result = TOOLS[tool_name](**args)
            except Exception as exc:
                result = f"XATO: Vosita bajarilmadi: {exc}"

            _show_preview(result)
            if native_tool_calls:
                history.append(
                    ChatMessage(role="tool", tool_name=tool_name, content=result)
                )
            else:
                history.append(
                    ChatMessage(role="user", content=f"Vosita natijasi: {result}")
                )


def main() -> None:
    """Terminaldagi interaktiv suhbatni boshlaydi va exit buyruqlarini boshqaradi."""
    try:
        settings = load_config()
    except ConfigurationError as exc:
        print(f"XATO: Konfiguratsiya noto'g'ri.\n{exc}")
        return

    ollama_client = OllamaClient(
        model=settings.ollama.model,
        host=settings.ollama.host,
        timeout=settings.ollama.timeout,
        max_retries=settings.agent.max_retries,
    )
    print(
        f"Shaxsiy AI agent tayyor ({settings.ollama.model}). "
        "Chiqish uchun exit deb yozing."
    )
    try:
        while True:
            try:
                task = input("Siz: ").strip()
            except EOFError:
                print("\nXayr!")
                return
            if task.casefold() in {"exit", "quit", "chiqish"}:
                print("Xayr!")
                return
            if task:
                run_task(
                    task,
                    model=settings.ollama.model,
                    client=ollama_client,
                )
    except KeyboardInterrupt:
        print("\nXayr!")


if __name__ == "__main__":
    main()
