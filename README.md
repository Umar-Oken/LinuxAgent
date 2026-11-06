# Shaxsiy AI agent

## O'rnatish va ishga tushirish

Ubuntu'da Python 3.10+ va [Ollama](https://ollama.com/) o'rnating, keyin:

```bash
ollama pull gemma4:e2b
python3 -m pip install -r requirements.txt
python3 agent.py
```

Ollama xizmatini alohida terminalda `ollama serve` bilan ishga tushiring (agar u avtomatik ishlamayotgan bo'lsa). Agent bilan `Siz: ` so'rov satrida gaplashing; `exit`, `quit`, `chiqish`, Ctrl+C yoki Ctrl+D bilan chiqing.

## Konfiguratsiya

Ixtiyoriy `config.yaml` faylini `$XDG_CONFIG_HOME/personal-ai-agent/config.yaml`
ichiga joylashtiring (`XDG_CONFIG_HOME` o'rnatilmagan bo'lsa
`~/.config/personal-ai-agent/config.yaml`). Agent ishga tushganda sozlamalar
tekshiriladi; noto'g'ri qiymatlarda agent tushunarli xato bilan ishga tushmaydi.
Fayl bo'lmasa xavfsiz standart qiymatlar qo'llanadi. Ma'lumotlar uchun XDG
yo'li `$XDG_DATA_HOME/personal-ai-agent` yoki sukut bo'yicha
`~/.local/share/personal-ai-agent`.

Masalan:

```yaml
ollama:
  host: http://127.0.0.1:11434
  model: gemma4:e2b
  timeout: 60
agent:
  max_steps: 8
  max_tool_calls: 8
  max_retries: 2
security:
  allowed_roots: []
  protected_paths:
    - ~/.ssh
    - ~/.gnupg
  approval_policy: writes
tools:
  enabled_categories: []
logging:
  level: INFO
  retention: 30
```

Qo'llab-quvvatlanadigan environment override'lari `AGENT_` prefiksidan,
bo'lim nomidan va maydon nomidan tuziladi: masalan,
`AGENT_OLLAMA_MODEL=gemma4:e2b` yoki `AGENT_AGENT_MAX_STEPS=10`.
Ro'yxat qiymatlari JSON massiv bo'lishi kerak, masalan
`AGENT_SECURITY_ALLOWED_ROOTS='["/home/user/work"]'`. Environment qiymati YAML
qiymatidan ustun turadi. Parol, token va boshqa maxfiy qiymatlarni YAML'ga
qo'shmang; noma'lum maydonlar konfiguratsiya xatosi hisoblanadi.

`security`, `tools` va agent limitlari hozircha faqat yuklanib tekshiriladi;
ularni vosita bajarilishida qo'llash keyingi alohida vazifa. Bu bosqichda
mavjud vositalarning ruxsat va bajarilish mantig'i o'zgarmaydi.

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

Agent `core/llm_client.py` adapteri orqali Ollama bilan muloqot qiladi. Model native tool calling'ni e'lon qilgan bo'lsa, shu format ishlatiladi; aks holda sxema bilan cheklangan JSON javobi qo'llanadi. Vosita argumentlari bajarilishidan oldin JSON Schema bo'yicha tekshiriladi. Vaqtinchalik ulanish xatolari konfiguratsiyadagi retry chegarasigacha qayta uriniladi; boshqa model yoki transport xatolari alohida ko'rsatiladi. Bajarilgan vosita natijalari suhbat tarixiga qaytariladi. Har bir qadam va qisqa natija terminalda ko'rinadi; bitta vazifada ko'pi bilan 8 vosita chaqiruvi bajariladi. Har qanday fayl yozishdan oldin foydalanuvchi `ha` deb tasdiqlashi kerak. Hisoblash faqat AST ruxsat ro'yxatidagi amallar orqali bajariladi.

## Demo

Quyidagi terminal chiqishini shu yerga joylashtiring:

```text
Siz:
```
