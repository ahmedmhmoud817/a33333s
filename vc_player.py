import asyncio
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from telebot import types

try:
    import yt_dlp
    HAS_YTDLP = True
except Exception:
    yt_dlp = None
    HAS_YTDLP = False

try:
    from pytgcalls import PyTgCalls
    from pytgcalls.types import MediaStream, AudioQuality, VideoQuality
    HAS_PYTGCALLS = True
except Exception:
    PyTgCalls = None
    MediaStream = None
    AudioQuality = None
    VideoQuality = None
    HAS_PYTGCALLS = False


class YouTubeCallScreenPlayer:
    """
    وحدة مستقلة لبحث يوتيوب وتشغيل الفيديوهات/الأغاني في كول الجروب (سكرين/فيديو)
    بدون تخزين ملفات على السيرفر، مع الحفاظ على استقرار بوت telebot الأساسي.
    """

    def __init__(self, bot, api_id=None, api_hash=None, session_string=None):
        self.bot = bot
        self.api_id = api_id or os.getenv("API_ID")
        self.api_hash = api_hash or os.getenv("API_HASH")
        self.session_string = session_string or os.getenv("TG_SESSION")

        self.search_waiting = {}   # user_id -> {"chat_id": int, "created_at": float}
        self.user_results = {}     # user_id -> list[dict]
        self.lock = threading.Lock()

        self._loop = asyncio.new_event_loop()
        self._vc_client = None
        self._vc_ready = False
        self._vc_error = None

        self._start_background_loop()
        self._init_vc_if_configured()
        self._register_handlers()

    def _start_background_loop(self):
        def _runner():
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()

        t = threading.Thread(target=_runner, daemon=True)
        t.start()

    def _run_coro(self, coro, timeout=20):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def _init_vc_if_configured(self):
        if not HAS_PYTGCALLS:
            self._vc_error = "مكتبة py-tgcalls غير متوفرة في البيئة الحالية."
            return
        if not (self.api_id and self.api_hash and self.session_string):
            self._vc_error = "يلزم ضبط API_ID و API_HASH و TG_SESSION في المتغيرات لتفعيل دخول الكول وفتح السكرين."
            return

        async def _setup():
            try:
                from pyrogram import Client
                user_client = Client(
                    name="vc_stream_session",
                    api_id=int(self.api_id),
                    api_hash=str(self.api_hash),
                    session_string=str(self.session_string),
                    in_memory=True,
                )
                vc = PyTgCalls(user_client)
                await vc.start()
                self._vc_client = vc
                self._vc_ready = True
                self._vc_error = None
            except Exception as exc:
                self._vc_ready = False
                self._vc_error = f"تعذر تشغيل عميل الكول: {exc}"

        try:
            self._run_coro(_setup(), timeout=25)
        except Exception as exc:
            self._vc_ready = False
            self._vc_error = f"خطأ أثناء تهيئة الكول: {exc}"

    @staticmethod
    def _escape_html(text):
        return (
            (text or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    @staticmethod
    def _format_duration(seconds):
        if not seconds:
            return "غير محدد"
        try:
            sec = int(seconds)
            hrs, rem = divmod(sec, 3600)
            mins, secs = divmod(rem, 60)
            if hrs > 0:
                return f"{hrs}:{mins:02d}:{secs:02d}"
            return f"{mins:02d}:{secs:02d}"
        except Exception:
            return "غير محدد"

    def search_youtube(self, query, limit=6):
        results = []
        if HAS_YTDLP:
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": True,
                "skip_download": True,
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
                    for idx, item in enumerate((info or {}).get("entries") or []):
                        if not item:
                            continue
                        vid = item.get("id") or ""
                        url = item.get("webpage_url") or item.get("url") or ""
                        if vid and not url.startswith("http"):
                            url = f"https://www.youtube.com/watch?v={vid}"
                        if not url:
                            continue
                        results.append({
                            "index": idx,
                            "id": vid,
                            "title": item.get("title") or "بدون عنوان",
                            "channel": item.get("uploader") or item.get("channel") or "YouTube",
                            "duration": self._format_duration(item.get("duration")),
                            "url": url,
                        })
            except Exception as exc:
                print(f"[YT_SEARCH_ERR] {exc}")

        if not results:
            try:
                q = urllib.parse.quote_plus(query)
                req = urllib.request.Request(
                    f"https://www.youtube.com/results?search_query={q}",
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
                    },
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    html = resp.read().decode("utf-8", errors="ignore")
                matches = re.findall(
                    r'"videoId":"([a-zA-Z0-9_-]{11})".*?"title":\{"runs":\[\{"text":"([^"]+)"\}\]',
                    html,
                )
                seen = set()
                for vid, raw_title in matches:
                    if vid in seen:
                        continue
                    seen.add(vid)
                    clean_title = raw_title.encode("utf-8").decode("unicode_escape", errors="ignore")
                    results.append({
                        "index": len(results),
                        "id": vid,
                        "title": clean_title or "نتيجة يوتيوب",
                        "channel": "YouTube",
                        "duration": "غير محدد",
                        "url": f"https://www.youtube.com/watch?v={vid}",
                    })
                    if len(results) >= limit:
                        break
            except Exception as exc:
                print(f"[YT_FALLBACK_ERR] {exc}")

        return results[:limit]

    def _resolve_stream_url(self, watch_url):
        """
        يجلب رابط بث مباشر من يوتيوب بدون حفظ أي ملفات على القرص.
        """
        if not HAS_YTDLP:
            return watch_url
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "format": "best[ext=mp4]/best",
            "noplaylist": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(watch_url, download=False)
                if isinstance(info, dict):
                    return info.get("url") or watch_url
        except Exception as exc:
            print(f"[STREAM_RESOLVE_ERR] {exc}")
        return watch_url

    def _stream_in_call(self, chat_id, stream_url):
        if not self._vc_ready or not self._vc_client or not MediaStream:
            return False, self._vc_error or "خدمة الكول غير مفعلة."

        async def _play():
            media = MediaStream(
                stream_url,
                audio_parameters=AudioQuality.STUDIO,
                video_parameters=VideoQuality.HD_720p,
            )
            await self._vc_client.play(chat_id, media)

        try:
            self._run_coro(_play(), timeout=25)
            return True, "تم فتح السكرين وبدء البث داخل الكول بدون تخزين."
        except Exception as exc:
            return False, f"تعذر بدء البث في الكول: {exc}"

    def _stop_in_call(self, chat_id):
        if not self._vc_ready or not self._vc_client:
            return False, "خدمة الكول غير نشطة."

        async def _stop():
            await self._vc_client.leave_call(chat_id)

        try:
            self._run_coro(_stop(), timeout=15)
            return True, "تم إيقاف البث ومغادرة الكول."
        except Exception as exc:
            return False, f"تعذر إيقاف البث: {exc}"

    def _build_search_box_markup(self, user_id):
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton(
                "🔍 اكتب اسم الفيلم أو الأغنية هنا",
                callback_data=f"ytbox_prompt:{user_id}"
            )
        )
        return markup

    def _build_results_markup(self, user_id, results):
        markup = types.InlineKeyboardMarkup(row_width=1)
        for item in results:
            idx = item["index"]
            short_title = (item["title"] or "تشغيل")[:28]
            markup.add(
                types.InlineKeyboardButton(
                    f"▶️ تشغيل: {short_title}",
                    callback_data=f"ytbox_play:{user_id}:{idx}"
                )
            )
        markup.add(
            types.InlineKeyboardButton("🔎 بحث جديد", callback_data=f"ytbox_prompt:{user_id}"),
            types.InlineKeyboardButton("⏹ إيقاف الكول", callback_data=f"ytbox_stop:{user_id}"),
        )
        return markup

    def _send_search_results(self, chat_id, user_id, query):
        wait_msg = self.bot.send_message(
            chat_id,
            f"🔎 <b>جاري البحث في يوتيوب عن:</b> <code>{self._escape_html(query)}</code>...",
            parse_mode="HTML"
        )
        results = self.search_youtube(query, limit=5)

        try:
            self.bot.delete_message(chat_id, wait_msg.message_id)
        except Exception:
            pass

        if not results:
            self.bot.send_message(
                chat_id,
                "❌ ملقتش نتائج واضحة دلوقتي، جرب اسم تاني.",
                reply_markup=self._build_search_box_markup(user_id)
            )
            return

        with self.lock:
            self.user_results[user_id] = results

        lines = [
            f"📺 <b>قائمة نتائج يوتيوب لـ:</b> <code>{self._escape_html(query)}</code>",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        for i, r in enumerate(results, start=1):
            lines.append(
                f"{i}. <b>{self._escape_html(r['title'])}</b>\n"
                f"   └ 🎙 {self._escape_html(r['channel'])} • ⏱ <code>{self._escape_html(r['duration'])}</code>"
            )
        lines.append("\nاختار زر <b>تشغيل</b> تحت النتيجة اللي عايزها 👇")

        self.bot.send_message(
            chat_id,
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=self._build_results_markup(user_id, results)
        )

    def _register_handlers(self):
        @self.bot.message_handler(
            func=lambda m: bool(
                m.text
                and m.text.strip().lower() in ["يوتيوب", "بحث", "بحث يوتيوب", "شغل", "فيلم", "اغنية", "أغنية"]
            )
        )
        def open_yt_search_box(message):
            uid = message.from_user.id if message.from_user else 0
            text = (
                "📺 <b>شريط بحث يوتيوب (تشغيل في الكول بدون تخزين)</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "اكتب اسم الفيلم أو الأغنية مباشرة:\n"
                "• إما بالضغط على الزر تحت وإرسال الاسم\n"
                "• أو بكتابة: <code>شغل اسم الفيلم أو الأغنية</code>"
            )
            self.bot.send_message(
                message.chat.id,
                text,
                parse_mode="HTML",
                reply_markup=self._build_search_box_markup(uid)
            )

        @self.bot.message_handler(
            func=lambda m: bool(
                m.text
                and (
                    m.text.strip().lower().startswith("شغل ")
                    or m.text.strip().lower().startswith("يوتيوب ")
                    or m.text.strip().lower().startswith("بحث ")
                )
            )
        )
        def direct_yt_search(message):
            raw = message.text.strip()
            parts = raw.split(" ", 1)
            query = (parts[1] if len(parts) > 1 else "").strip()
            uid = message.from_user.id if message.from_user else 0
            if not query:
                self.bot.send_message(
                    message.chat.id,
                    "اكتب اسم الفيلم أو الأغنية بعد الكلمة.",
                    reply_markup=self._build_search_box_markup(uid)
                )
                return
            self._send_search_results(message.chat.id, uid, query)

        @self.bot.message_handler(
            func=lambda m: bool(m.from_user and m.from_user.id in self.search_waiting),
            content_types=["text"],
        )
        def handle_waiting_search_query(message):
            uid = message.from_user.id
            with self.lock:
                st = self.search_waiting.get(uid)
                if not st or st["chat_id"] != message.chat.id:
                    return
                if time.time() - st["created_at"] > 120:
                    del self.search_waiting[uid]
                    return
                del self.search_waiting[uid]

            query = (message.text or "").strip()
            if not query:
                return
            self._send_search_results(message.chat.id, uid, query)

        @self.bot.callback_query_handler(
            func=lambda call: bool(call.data and call.data.startswith("ytbox_prompt:"))
        )
        def callback_prompt_query(call):
            uid = call.from_user.id if call.from_user else 0
            with self.lock:
                self.search_waiting[uid] = {
                    "chat_id": call.message.chat.id,
                    "created_at": time.time(),
                }
            self.bot.answer_callback_query(call.id, "اكتب اسم الفيلم أو الأغنية في الشات ✍️")
            self.bot.send_message(
                call.message.chat.id,
                "✍️ <b>اكتب دلوقتي اسم الفيلم أو الأغنية اللي عايز تشغلها:</b>",
                parse_mode="HTML"
            )

        @self.bot.callback_query_handler(
            func=lambda call: bool(call.data and call.data.startswith("ytbox_play:"))
        )
        def callback_play_item(call):
            parts = call.data.split(":")
            if len(parts) != 3:
                return
            owner_id = int(parts[1])
            idx = int(parts[2])
            clicker_id = call.from_user.id if call.from_user else 0

            with self.lock:
                results = self.user_results.get(owner_id) or self.user_results.get(clicker_id) or []
            chosen = next((r for r in results if r["index"] == idx), None)

            if not chosen:
                self.bot.answer_callback_query(call.id, "النتيجة انتهت، ابحث من جديد.", show_alert=True)
                return

            self.bot.answer_callback_query(call.id, f"جاري تجهيز التشغيل: {chosen['title'][:25]}...")

            stream_url = self._resolve_stream_url(chosen["url"])
            ok, status_msg = self._stream_in_call(call.message.chat.id, stream_url)

            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🎬 فتح على يوتيوب", url=chosen["url"]))

            if ok:
                card = (
                    f"📡 <b>جاري البث في الكول (سكرين/فيديو بدون تخزين)</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>العنوان:</b> {self._escape_html(chosen['title'])}\n"
                    f"• <b>القناة:</b> {self._escape_html(chosen['channel'])}\n"
                    f"• <b>المدة:</b> <code>{self._escape_html(chosen['duration'])}</code>\n"
                    f"• <b>الحالة:</b> {self._escape_html(status_msg)}"
                )
            else:
                card = (
                    f"▶️ <b>تم تجهيز التشغيل</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>العنوان:</b> {self._escape_html(chosen['title'])}\n"
                    f"• <b>القناة:</b> {self._escape_html(chosen['channel'])}\n"
                    f"• <b>المدة:</b> <code>{self._escape_html(chosen['duration'])}</code>\n"
                    f"⚠️ <i>{self._escape_html(status_msg)}</i>"
                )

            self.bot.send_message(call.message.chat.id, card, parse_mode="HTML", reply_markup=markup)

        @self.bot.callback_query_handler(
            func=lambda call: bool(call.data and call.data.startswith("ytbox_stop:"))
        )
        def callback_stop_call(call):
            ok, msg = self._stop_in_call(call.message.chat.id)
            self.bot.answer_callback_query(call.id, msg[:180], show_alert=not ok)
            self.bot.send_message(call.message.chat.id, f"⏹ <b>{self._escape_html(msg)}</b>", parse_mode="HTML")


def setup_youtube_call_player(bot):
    return YouTubeCallScreenPlayer(bot)
