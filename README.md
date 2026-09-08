# Hermes Soroush Plus Plugin (سروش پلاس)

اتصال هرمس به پیام‌رسان سروش‌پلاس به‌عنوان پلتفرم (پلاگین) — **حالت ربات**.

## روش کار

سروش‌پلاس یک Bot API رسمی و عمومی دارد (شبیه بله/تلگرام):

```
https://api.splus.ir/bot<TOKEN>/<method>
```

این پلاگین **مستقیم** با همین Bot API کار می‌کند (HTTP + JSON با aiohttp) —
هیچ وابستگی SDK اضافه‌ای ندارد (برخلاف PySPlusthon که سنگین و باگ‌دار بود).

مستندات رسمی: https://soroushplus.com/p/documents/bot-platform

## گرفتن توکن ربات

سروش‌پلاس **ربات‌ساز رسمی** دارد:

- **https://splus.ir/botfather** — ربات‌ساز رسمی سروش (توکن می‌دهد)

## امکانات

- متن (ارسال و دریافت)
- **نشانگر «در حال نوشتن...»** (typing indicator — `sendChatAction`)
- عکس — ارسال با URL یا فایل محلی
- صدا (voice/audio) — ارسال و دریافت
- ویدیو — ارسال و دریافت
- فایل/مستند — ارسال و دریافت
- پشتیبانی از گروه و کانال (با گیت اختیاری @mention)
- لیست مجاز کاربران/چت‌ها (allowlist)
- تحویل پیام‌های cron (standalone sender)

## نصب

```bash
# clone
cd ~/.hermes/plugins/platforms/
git clone https://github.com/mah92/hermes-sorush-messenger-plugin.git soroush

# فعال‌سازی
hermes plugins enable hermes-sorush-messenger
```

وابستگی: فقط `aiohttp` (که معمولاً نصب است).

## تنظیم

توکن را به `~/.hermes/.env` اضافه کنید:

```env
SOROUSH_BOT_TOKEN=12345:ABC...
SOROUSH_HOME_CHANNEL=<chat_id اختیاری>
SOROUSH_ALLOWED_USERS=<user_id خودت>  # تا فقط خودت مجاز باشی
```

سپس گیتوی را ریاستارت کنید.

## متغیرهای محیطی

| متغیر | الزامی | توضیح |
|-------|--------|--------|
| `SOROUSH_BOT_TOKEN` | ✅ | توکن ربات سروش (از splus.ir/botfather) |
| `SOROUSH_HOME_CHANNEL` | خیر | چت پیش‌فرض برای cron |
| `SOROUSH_ALLOWED_CHATS` | خیر | فقط این چت‌ها مجازند |
| `SOROUSH_ALLOWED_USERS` | خیر | فقط این کاربران مجازند |
| `SOROUSH_ALLOW_ALL_USERS` | خیر | `true` = همه مجاز |
| `SOROUSH_REQUIRE_MENTION` | خیر | `true` = در گروه‌ها فقط با @mention جواب بده |

## نکات

- سروش فقط متن ساده پشتیبانی می‌کند — پلاگین مارک‌داون را پاک می‌کند.
- ربات نمی‌تواند اول پیام دهد — کاربر باید اول به ربات پیام بفرستد.
- اگر پلاگین بوت نشد لاگ را ببینید: `hermes gateway status` / `journalctl -u hermes-gateway -n 50`
