# Gate.io Spot Pro Scalper — تطبيق أندرويد (Kivy + Buildozer)

تطبيق جوال متكامل لسوق السبوت على منصة **Gate.io (API v4)** مبني بـ **Kivy** وباعتماديات بايثون نقية 100% (بدون `ccxt` أو `pandas` أو `numpy`) لضمان بناء ملف **APK** سريع ومستقر بدون أخطاء ترجمة.

## بناء ملف APK تلقائياً عبر GitHub Actions (3 خطوات)
1. أنشئ مستودعاً جديداً على **GitHub** وارفع جميع ملفات هذا المجلد (بما فيها مجلد `.github/workflows/build_apk.yml` وملف `buildozer.spec`).
2. انتقل إلى تبويب **Actions** في المستودع واضغط **Run workflow** (أو انتظر البناء التلقائي بعد الرفع).
3. بعد اكتمال البناء، حمّل ملف `gateio-spot-pro-scalper-apk` من قسم **Artifacts** وثبّته على هاتفك الأندرويد.

## تجربة التطبيق على الكمبيوتر قبل بناء APK
```bash
pip install -r requirements.txt
python main.py
```

## المواصفات التقنية
- **معمارية الأندرويد:** `armeabi-v7a` (API 33، NDK 28c) متوافقة مع معظم أجهزة الأندرويد.
- **المؤشرات:** EMA 9/21/200، Daily Anchored VWAP، انفجار الفوليوم + Z-Score، وWilder RSI(14) مكتوبة بالكامل ببايثون نقي.
- **إدارة التداول:** حساب تجريبي (Paper) وحقيقي (Live) عبر Gate.io API v4 (`HMAC-SHA512`)، مبلغ الصفقة، أقصى عدد صفقات، `TP%`، `SL%`، `Trailing SL`، وتنبيهات Telegram.
