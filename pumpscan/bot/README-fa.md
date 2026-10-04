# ربات F4 برای Robinhood Chain (از طریق API رسمی GMGN)

ربات هر ۶۰ ثانیه لیست Trending در GMGN را با فیلتر F4 می‌گیرد: Robinhood، تایم‌فریم 1h، Liq ≥ 70K، ATH MC < 160K، 1h Vol ≥ 70K، 1h TXs < 900، بدون هانی‌پات.
هر توکنی را که **برای اولین بار** وارد لیست شود، **یک بار** با مبلغ ثابت می‌خرد.

**قانون خروج:** همراه هر خرید در خود GMGN ثبت می‌شود:
- در +100% نصف فروخته می‌شود؛
- روی بقیه تریلینگ 35%؛
- حد ضرر -35%؛
- هرچه بعد از ۶ ساعت مانده باشد را خود ربات می‌فروشد.

**پیش‌فرض: حالت آزمایشی (`DRY_RUN=1`).** در این حالت هیچ خرید واقعی انجام نمی‌شود و فقط گزارش می‌دهد چه چیزی می‌خرید.

## فقط از این منابع استفاده کنید (ضد اسکم)
- سایت GMGN: **https://gmgn.ai** — صفحه‌ی API: **https://gmgn.ai/ai**
- ابزار رسمی: پکیج npm به اسم **`gmgn-cli`**
  - منتشرکننده: `infra@gmgn.ai`
  - سورس: **https://github.com/GMGNAI/gmgn-skills**
  - نسخه‌ی قفل‌شده در `package.json`: `1.6.6`
- تلگرام: فقط **@BotFather** (ربات رسمی خود تلگرام) برای ساخت ربات.
- هر سایت، ربات یا لینک دیگری که «API کلید GMGN» یا «ربات آماده» می‌فروشد، **اسکم است**.
- کلیدها را به هیچ‌کس، از جمله من، نفرستید. فقط روی سرور خودتان در فایل `.env` بمانند.

## مراحل نصب روی سرور (Ubuntu 24)

### ۱. نصب Node.js 22
```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs git
node -v   # باید v22 باشد
```

### ۲. گرفتن کد و نصب
```bash
git clone --filter=blob:none --sparse -b claude/beautiful-mccarthy-d1pizl https://github.com/Thedudea/Base.git
cd Base && git sparse-checkout set pumpscan/bot pumpscan/analysis pumpscan/scan pumpscan/results
cd pumpscan/bot
npm install
```
این روش حدود ۷۰۰ مگابایت داده‌ی خام اسکنر را دانلود نمی‌کند. راهنمای کامل پروژه در `pumpscan/HANDOFF.md` است.
اگر ریپو private است، `git clone` نام کاربری GitHub و یک Personal Access Token می‌خواهد.

### ۳. ساخت کلید و گرفتن API Key از GMGN
```bash
npx gmgn-cli config
```
- این دستور روی همین سرور یک جفت کلید می‌سازد و یک لینک به **gmgn.ai** می‌دهد.
- لینک را باز کنید و با حساب GMGN خودتان API Key بسازید.
- **مطمئن شوید آدرس دقیقاً `gmgn.ai` است.**

بعد کلیدی را که GMGN داد این‌طور ثبت کنید:
```bash
npx gmgn-cli config --apply <API_KEY>
npx gmgn-cli config --check        # باید OK بدهد
```
کلیدها در `~/.config/gmgn/.env` ذخیره می‌شوند.

### ۴. آدرس کیف‌پول
```bash
npx gmgn-cli portfolio info
```
- آدرس کیف‌پول **Robinhood** را از خروجی بردارید.
- **پیشنهاد:** در GMGN یک کیف‌پول جدا فقط برای ربات بسازید و فقط بودجه‌ی ربات را در آن بگذارید.

### ۵. تنظیمات
```bash
cp .env.example .env
nano .env
```
- `WALLET=` آدرس مرحله‌ی ۴.
- `BUY_ETH=` مبلغ هر خرید.
- `MAX_BUYS_PER_DAY=` سقف تعداد خرید در روز.
- `REQUIRE_DEX_PAID=1` فقط اگر نسخه‌ی «F4 + دکس پید» را می‌خواهید.
- `DRY_RUN=1` را فعلاً عوض نکنید.

### ۶. تلگرام (اختیاری)
1. در تلگرام به **@BotFather** پیام بدهید: `/newbot`. توکن را در `TELEGRAM_BOT_TOKEN` بگذارید.
2. به ربات جدیدتان یک پیام بدهید، بعد روی سرور:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":[0-9-]*'
   ```
   عدد id را در `TELEGRAM_CHAT_ID` بگذارید.
3. دستورها:
   - `/status` وضعیت؛
   - `/pause` توقف خرید؛
   - `/resume` ادامه.

   فقط چت خودتان می‌تواند ربات را کنترل کند.

### ۷. اجرای آزمایشی (حداقل یک روز)
```bash
node bot.mjs
```
- هر سیگنال در صفحه و تلگرام با «[آزمایشی] خرید می‌شد» نشان داده می‌شود و در `trades.csv` ثبت می‌شود.
- توکن‌هایی که موقع روشن شدن ربات **از قبل** در لیست بوده‌اند خریده نمی‌شوند، چون سیگنال تازه نیستند.

### ۸. اجرای دائمی با systemd
```bash
sudo tee /etc/systemd/system/f4bot.service >/dev/null <<EOF
[Unit]
Description=F4 bot
After=network-online.target
[Service]
WorkingDirectory=$PWD
ExecStart=$(which node) bot.mjs
Restart=always
RestartSec=10
User=$USER
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now f4bot
journalctl -u f4bot -f        # دیدن گزارش زنده
```

### ۹. روشن کردن خرید واقعی
وقتی از حالت آزمایشی راضی بودید:
1. در `.env` مقدار `DRY_RUN=0` را بگذارید.
2. اجرا کنید: `sudo systemctl restart f4bot`.

**اول با مبلغ خیلی کم** شروع کنید.

## نکته‌ها
- **کارمزد:** هر خرید و فروش از طریق GMGN همان **۱٪** معمول را دارد.
- **سقف درخواست:** پلن رایگان API محدودیت تعداد درخواست دارد. ربات هر ۶۰ ثانیه فقط یک درخواست لیست می‌فرستد که کافی است.
- **فایل‌های محرمانه:** `.env`، `state.json` و `trades.csv` در گیت ذخیره نمی‌شوند.
- **قبل از دلار واقعی** حتماً در حالت آزمایشی ببینید سیگنال‌ها و دستورهای فروش همان چیزی است که انتظار دارید. اولین خرید واقعی را با کمترین مبلغ انجام دهید و چک کنید TP/SL و تریلینگ در GMGN درست ثبت شده باشد.
