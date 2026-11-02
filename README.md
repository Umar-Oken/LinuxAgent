# Shaxsiy AI agent

## O'rnatish va ishga tushirish

Ubuntu'da Python 3.10+ va [Ollama](https://ollama.com/) o'rnating, keyin:

```bash
ollama pull gemma4:e2b
python3 -m pip install ollama psutil
python3 agent.py
```

Ollama xizmatini alohida terminalda `ollama serve` bilan ishga tushiring (agar u avtomatik ishlamayotgan bo'lsa). Agent bilan `Siz: ` so'rov satrida gaplashing; `exit`, `quit`, `chiqish`, Ctrl+C yoki Ctrl+D bilan chiqing.

## Vositalar

| Vosita | Vazifasi |
|---|---|
| `find_files` | Yashirin papkalarni chetlab, nom bo'lagi yoki kengaytma bilan 20 tagacha fayl qidiradi. |
| `read_text` | `.txt` va `.md` fayllarni UTF-8 formatida, 6000 belgigacha o'qiydi. |
| `save_note` | To'liq matn va yo'lni ko'rsatib, tasdiqdan keyin `~/agent_notes/` ichiga yozadi. |
| `calculate` | AST ruxsat ro'yxati orqali xavfsiz arifmetik amallarni hisoblaydi. |
| `system_status` | CPU, mantiqiy yadro, RAM va `/` disk holatini ko'rsatadi. |
| `get_weather` | API kalitisiz Open-Meteo'dan joriy ob-havoni oladi. |

## Talablar qanday bajariladi

Model JSON protokoli bilan vositani tanlaydi; agent `ollama.chat(..., format="json")` javobini tekshiradi, bajarilgan vosita natijasini to'liq suhbat tarixiga qo'shib, modelga qaytaradi. Noto'g'ri JSON va noma'lum vositalar xato sifatida modelga qaytariladi. Har bir qadam va qisqa natija terminalda ko'rinadi; bitta vazifada ko'pi bilan 8 vosita qadami bajariladi. Har qanday fayl yozishdan oldin foydalanuvchi `ha` deb tasdiqlashi kerak. Hisoblash faqat AST ruxsat ro'yxatidagi amallar orqali bajariladi; foizli masalalar model tomonidan arifmetik ifodaga aylantiriladi.

## Demo

Quyidagi terminal chiqishini shu yerga joylashtiring:

```text
Siz:
```
