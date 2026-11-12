# Shaxsiy AI agent

## O'rnatish va ishga tushirish

Ubuntu'da Python 3.10+ va [Ollama](https://ollama.com/) o'rnating, keyin:

```bash
ollama pull gemma4:e2b-it-qat
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
  model: gemma4:e2b-it-qat
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
`AGENT_OLLAMA_MODEL=gemma4:e2b-it-qat` yoki
`AGENT_AGENT_MAX_STEPS=10`.
Ro'yxat qiymatlari JSON massiv bo'lishi kerak, masalan
`AGENT_SECURITY_ALLOWED_ROOTS='["/home/user/work"]'`. Environment qiymati YAML
qiymatidan ustun turadi. Parol, token va boshqa maxfiy qiymatlarni YAML'ga
qo'shmang; noma'lum maydonlar konfiguratsiya xatosi hisoblanadi.
`network.connectivity_endpoint` (yoki `AGENT_NETWORK_CONNECTIVITY_ENDPOINT`)
ulanish tekshiruvi uchun ishonchli HTTPS endpointni belgilaydi; qiymat
foydalanuvchi konfiguratsiyasidan olinadi va model tomonidan boshqarilmaydi.
`network.exchange_rate_providers` (`AGENT_NETWORK_EXCHANGE_RATE_PROVIDERS`)
valyuta kursi API'larining HTTPS endpointlar ro'yxatini,
`web_timeout_seconds` va `web_max_content_bytes` esa veb-so'rov limitlarini
belgilaydi. Kurs endpointlari `from` va `to` query parametrlarini qabul qilishi
va JSON'da `rates` xaritasini (Frankfurter uslubi) yoki
`conversion_rate`/`conversion_rates` maydonlarini qaytarishi kerak.

`security.allowed_roots` fayl vositalari uchun aniq ruxsat etilgan kataloglarni
belgilaydi. `tools.enabled_categories` registry'da e'lon qilinadigan tool
kategoriyalarini cheklaydi. Eslatma yozish `CONTROLLED` amal bo'lib, ishonchli
CLI orqali tasdiqlanishi kerak.

## Vositalar

| Vosita | Vazifasi |
|---|---|
| `search_files` | Yashirin papkalarni chetlab, nom bo'lagi yoki kengaytma bilan 20 tagacha fayl qidiradi. |
| `list_directory` | Ruxsat etilgan katalog tarkibini saralash va sahifalash bilan ko'rsatadi. |
| `search_files_advanced` | Fayl andozasi, kengaytma, hajm va o'zgartirilgan sana bo'yicha cheklangan rekursiv qidiruv. |
| `get_file_metadata` | Ruxsat etilgan regular faylning hajmi, vaqti va ruxsat bitlarini qaytaradi. |
| `find_large_files` | Belgilangan hajmdan katta fayllarni cheklangan rekursiya va pagination bilan qidiradi. |
| `copy_file` | Tasdiqdan keyin faylni mavjud fayl ustidan yozmasdan nusxalaydi. |
| `move_file` | Tasdiqdan keyin faylni ruxsat etilgan joyga ko'chiradi, ziddiyatda ustidan yozmaydi. |
| `rename_file` | Tasdiqdan keyin fayl nomini xavfsiz o'zgartiradi. |
| `create_directory` | Tasdiqdan keyin ruxsat etilgan root ichida yangi katalog yaratadi. |
| `organize_directory` | Fayllarni kengaytma qoidalari bilan tartiblash rejasini ko'rsatadi; `apply=true` va preview'dagi `plan_id` bilan tasdiqdan so'ng ko'chiradi. |
| `find_duplicate_files` | SHA-256 orqali bir xil fayllarni guruhlaydi; hech birini o'chirmaydi. |
| `create_archive` | Ruxsat etilgan katalogdan cheklangan ZIP arxivini tasdiq bilan yaratadi. |
| `extract_archive` | ZIP/TAR arxivini traversal, maxsus fayl va decompression-bomb cheklovlari bilan tasdiqdan so'ng ochadi. |
| `read_text_file` | `.txt` va `.md` fayllarni UTF-8 formatida, 6000 belgigacha o'qiydi. |
| `create_note` | To'liq matn va yo'lni ko'rsatib, tasdiqdan keyin `~/agent_notes/` ichiga yozadi. |
| `calculate` | AST ruxsat ro'yxati orqali xavfsiz arifmetik amallarni hisoblaydi. |
| `get_system_status` | CPU, mantiqiy yadro, RAM va `/` disk holatini ko'rsatadi. |
| `get_cpu_details` | CPU yuklanishi va mantiqiy/jismoniy yadro sonini ko'rsatadi. |
| `get_memory_details` | RAM va swap xotirasi ishlatilishini GiB va foizlarda ko'rsatadi. |
| `get_disk_details` | Ulangan fayl tizimlarining band/bo'sh joyini ko'rsatadi; natijalar limiti 1–100. |
| `list_processes` | Eng ko'p CPU yoki RAM ishlatayotgan jarayonlarni ko'rsatadi; limit 1–100, environment va buyruq satrini yig'maydi. |
| `get_network_interfaces` | Interfeys holati va trafik hisoblagichlarini ko'rsatadi; IP va MAC manzillarini yig'maydi. |
| `check_internet_connectivity` | Faqat sozlangan ishonchli HTTPS endpoint'ga 3 soniyalik timeout bilan bitta HEAD so'rov yuboradi. |
| `inspect_process` | PID orqali jarayon holati va CPU/xotira/thread sarfini ko'rsatadi; jarayonni o'zgartirmaydi. |
| `get_system_uptime` | Tizim ishga tushganidan beri o'tgan vaqtni va UTC boot vaqtini ko'rsatadi. |
| `get_weather` | API kalitisiz Open-Meteo'dan joriy ob-havoni oladi. |
| `fetch_webpage` | Public HTTP(S) HTML/matnni DNS-pinning, redirect tekshiruvi va hajm limiti bilan yuklab, ko'rinadigan 3000 belgigacha matnni chiqaradi. Sahifa mazmuni ishonchsiz. |
| `summarize_webpage` | Xavfsiz olingan sahifa matnidan qisqa lokal xulosa tuzadi; sayt ko'rsatmalariga amal qilmaydi. |
| `get_exchange_rate` | Sozlangan 1–5 ta HTTPS ochiq API provayderidan kurs oladi; muvaffaqiyatsiz providerda keyingisini sinaydi. |
| `check_website_status` | Public HTTP(S) saytdan cheklangan timeout bilan javob holatini tekshiradi; javob body'sini yuklab olmaydi. |

## Talablar qanday bajariladi

Agent `core/llm_client.py` adapteri orqali Ollama bilan muloqot qiladi. Model native tool calling'ni e'lon qilgan bo'lsa, shu format ishlatiladi; aks holda sxema bilan cheklangan JSON javobi qo'llanadi. Vosita argumentlari bajarilishidan oldin JSON Schema bo'yicha tekshiriladi. Vaqtinchalik ulanish xatolari konfiguratsiyadagi retry chegarasigacha qayta uriniladi; boshqa model yoki transport xatolari alohida ko'rsatiladi. Bajarilgan vosita natijalari suhbat tarixiga qaytariladi. Har bir qadam va qisqa natija terminalda ko'rinadi; bitta vazifada ko'pi bilan 8 vosita chaqiruvi bajariladi. Har qanday fayl yozishdan oldin foydalanuvchi `ha` deb tasdiqlashi kerak. Hisoblash faqat AST ruxsat ro'yxatidagi amallar orqali bajariladi.

## Demo

Quyidagi terminal chiqishini shu yerga joylashtiring:

```text
Siz:
```
