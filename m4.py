# m.py - Music Bot v3.1 PCM (Simple) - filters ON/OFF, no embed on filter/volume
# Requirements: pip install -U discord.py yt-dlp PyNaCl
# FFmpeg in PATH
# Put your token at the bottom

import discord
from discord.ext import commands
import yt_dlp as youtube_dl
import asyncio
import random
import time

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# -----------------------
# State
# -----------------------
queues = {}
history = {}
filters = {}        # main filter per guild: 'nightcore' | 'vaporwave' | 'eq:<preset>' | None
bass_gain = {}      # guild -> dB (int)
volumes = {}        # guild -> float (1.0 == 100%)
current_song = {}   # guild -> {url,title,webpage,thumb,duration,start_time,start_offset}
suppress_after = {} # guild -> bool
loop_mode = {}      # 0 none,1 single,2 queue
autoplay = {}
search_results = {}
control_message = {}

# -----------------------
# yt-dlp + ffmpeg base
# -----------------------
ydl_opts = {
    "format": "bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "default_search": "auto",
    "extract_flat": False
}

FFMPEG_RECONNECT = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_BASE = "-vn"

EQ_PRESETS = {
    "rock": "equalizer=f=60:width_type=h:width=100:g=4, equalizer=f=170:width_type=h:width=100:g=3",
    "pop": "equalizer=f=60:width_type=h:width=100:g=3, equalizer=f=1000:width_type=h:width=200:g=2",
    "jazz": "equalizer=f=200:width_type=h:width=150:g=2",
    "soft": "equalizer=f=100:width_type=h:width=150:g=1",
    "boost": "bass=g=8"
}

# -----------------------
# Helpers
# -----------------------
def get_queue(gid):
    return queues.setdefault(gid, [])

def get_history(gid):
    return history.setdefault(gid, [])

def now_time():
    return asyncio.get_event_loop().time()

def fetch_info(query_or_url):
    try:
        with youtube_dl.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query_or_url, download=False)
    except Exception:
        return None
    if not info:
        return None
    if isinstance(info, dict) and "entries" in info:
        if not info["entries"]:
            return None
        info = info["entries"][0]
    stream_url = None
    if info.get("url"):
        stream_url = info["url"]
    elif info.get("formats"):
        for f in info["formats"]:
            if f.get("url"):
                stream_url = f["url"]; break
    title = info.get("title", "Unknown")
    webpage = info.get("webpage_url", None)
    thumb = info.get("thumbnail", None)
    duration = info.get("duration", None)
    return stream_url, title, webpage, thumb, duration

def fetch_playlist_entries(playlist_url):
    entries = []
    try:
        with youtube_dl.YoutubeDL({**ydl_opts, "noplaylist": False}) as ydl:
            info = ydl.extract_info(playlist_url, download=False)
    except Exception:
        return entries
    if not info:
        return entries
    if "entries" in info:
        for it in info["entries"]:
            if not it: continue
            stream_url = it.get("url")
            if not stream_url and it.get("formats"):
                for f in it["formats"]:
                    if f.get("url"):
                        stream_url = f["url"]; break
            title = it.get("title", "Unknown")
            webpage = it.get("webpage_url", None)
            thumb = it.get("thumbnail", None)
            duration = it.get("duration", None)
            if stream_url:
                entries.append((stream_url, title, webpage, thumb, duration))
    return entries

# -----------------------
# Build filter string (volume, bass, main filter)
# -----------------------
def build_filter_string(gid):
    parts = []
    vol = volumes.get(gid, 1.0)
    if vol != 1.0:
        parts.append(f"volume={vol}")
    if gid in bass_gain:
        parts.append(f"bass=g={bass_gain[gid]}")
    eff = filters.get(gid)
    if eff == "nightcore":
        parts.append("asetrate=48000*1.25,aresample=48000")
    elif eff == "vaporwave":
        parts.append("asetrate=44100*0.85,aresample=44100")
    elif isinstance(eff, str) and eff.startswith("eq:"):
        preset = eff.split(":",1)[1]
        eq = EQ_PRESETS.get(preset)
        if eq:
            parts.append(eq)
    if not parts:
        return ""
    return ",".join(parts)

def build_ffmpeg_before_and_options(gid, start_offset=None):
    base_before = FFMPEG_RECONNECT
    filter_str = build_filter_string(gid)
    base_options = FFMPEG_BASE
    if filter_str:
        options = f'{base_options} -af "{filter_str}"'
        if start_offset:
            options = f'{options} -ss {int(start_offset)}'
        before = base_before
    else:
        before = base_before
        if start_offset:
            before = f"{before} -ss {int(start_offset)}"
        options = base_options
    return before, options

# -----------------------
# Playback / position
# -----------------------
def get_play_position(gid):
    info = current_song.get(gid)
    if not info:
        return 0.0
    start_time = info.get("start_time")
    start_offset = info.get("start_offset", 0.0)
    if start_time is None:
        return float(start_offset)
    return float(start_offset + (now_time() - start_time))

def after_wrapper(gid, ctx):
    def _after(err):
        try:
            if suppress_after.pop(gid, False):
                return
            fut = asyncio.run_coroutine_threadsafe(after_song(ctx), bot.loop)
            fut.result()
        except Exception:
            pass
    return _after

async def start_playback(ctx, stream_url, title, webpage=None, thumb=None, duration=None, start_offset=0.0, send_np=True):
    """Use PCM so filters + seek + restart-from-position work reliably."""
    gid = ctx.guild.id
    voice = ctx.voice_client
    if not voice:
        if ctx.author.voice:
            await ctx.author.voice.channel.connect()
            voice = ctx.voice_client
        else:
            return await ctx.send("❌ Musisz być na kanale głosowym.")
    current_song[gid] = {
        "url": stream_url,
        "title": title,
        "webpage": webpage,
        "thumb": thumb,
        "duration": duration,
        "start_time": now_time(),
        "start_offset": float(start_offset)
    }
    before, options = build_ffmpeg_before_and_options(gid, start_offset if start_offset else None)
    volume = volumes.get(gid, 1.0)
    try:
        ff = discord.FFmpegPCMAudio(stream_url, before_options=before, options=options)
    except Exception:
        ff = discord.FFmpegPCMAudio(stream_url)
    source = discord.PCMVolumeTransformer(ff, volume)
    voice.play(source, after=after_wrapper(gid, ctx))
    get_history(gid).append(title)
    if send_np:
        await send_now_playing(ctx, gid)

async def after_song(ctx):
    gid = ctx.guild.id
    q = get_queue(gid)
    v = ctx.voice_client
    mode = loop_mode.get(gid, 0)
    if mode == 1:
        info = current_song.get(gid)
        if info:
            await start_playback(ctx, info["url"], info["title"], info.get("webpage"), info.get("thumb"), info.get("duration"), start_offset=0.0)
            return
    if q:
        item = q.pop(0)
        if len(item) >= 5:
            url,title,web,thumb,dur = item
        else:
            url,title = item[0], item[1]; web=thumb=None; dur=None
        await start_playback(ctx, url, title, web, thumb, dur, start_offset=0.0)
        return
    if autoplay.get(gid, False):
        info = current_song.get(gid)
        if info and info.get("title"):
            title = info["title"]
            try:
                with youtube_dl.YoutubeDL(ydl_opts) as ydl:
                    data = ydl.extract_info(f"ytsearch5:{title}", download=False)
                chosen = None
                if data and "entries" in data:
                    for entry in data["entries"]:
                        if not entry: continue
                        t = entry.get("title","")
                        if t and t != title:
                            sub = fetch_info(entry.get("webpage_url") or entry.get("id") or entry.get("url"))
                            if sub:
                                chosen = sub
                                break
                if chosen:
                    url,title,web,thumb,dur = chosen
                    await start_playback(ctx, url,title,web,thumb,dur, start_offset=0.0)
                    return
            except Exception:
                pass
    # nothing next -> leave bot in VC, keep current_song

# -----------------------
# Buttons: PlayerView (only created by play/np)
# -----------------------
class PlayerView(discord.ui.View):
    def __init__(self, gid, ctx, *, timeout=180.0):
        super().__init__(timeout=timeout)
        self.gid = gid
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        member = guild.get_member(interaction.user.id)
        vc = guild.voice_client
        if not vc:
            await interaction.response.send_message("Bot nie jest połączony.", ephemeral=True)
            return False
        if member is None or not member.voice or member.voice.channel != vc.channel:
            await interaction.response.send_message("Musisz być na tym samym kanale głosowym, aby sterować.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="⏯️", style=discord.ButtonStyle.secondary)
    async def playpause(self, interaction: discord.Interaction, button: discord.ui.Button):
        v = interaction.guild.voice_client
        if not v:
            await interaction.response.send_message("❌ Bot nie jest połączony.", ephemeral=True); return
        if v.is_playing():
            v.pause(); await interaction.response.send_message("⏸ Wstrzymano.", ephemeral=True)
        elif v.is_paused():
            v.resume(); await interaction.response.send_message("▶ Wznowiono.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Nic nie gra.", ephemeral=True)

    @discord.ui.button(label="⏭️", style=discord.ButtonStyle.primary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        v = interaction.guild.voice_client
        if not v or not v.is_playing():
            await interaction.response.send_message("❌ Nic nie gra.", ephemeral=True); return
        v.stop(); await interaction.response.send_message("⏭ Pominięto.", ephemeral=False)

    @discord.ui.button(label="⏮️", style=discord.ButtonStyle.secondary)
    async def replay(self, interaction: discord.Interaction, button: discord.ui.Button):
        info = current_song.get(interaction.guild.id)
        if not info:
            await interaction.response.send_message("❌ Nic nie gra.", ephemeral=True); return
        suppress_after[interaction.guild.id] = True
        v = interaction.guild.voice_client
        if v: v.stop()
        await start_playback(self.ctx, info["url"], info["title"], info.get("webpage"), info.get("thumb"), info.get("duration"), start_offset=0.0)
        await interaction.response.send_message("⏮ Odtwarzam od początku.", ephemeral=False)

    @discord.ui.button(label="🔁", style=discord.ButtonStyle.secondary)
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        cur = loop_mode.get(gid,0); cur = (cur + 1) % 3; loop_mode[gid] = cur
        txt = "off" if cur==0 else ("single" if cur==1 else "queue")
        await interaction.response.send_message(f"🔁 Loop: {txt}", ephemeral=False)

    @discord.ui.button(label="🔀", style=discord.ButtonStyle.secondary)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        random.shuffle(get_queue(interaction.guild.id))
        await interaction.response.send_message("🔀 Kolejka wymieszana.", ephemeral=False)

    @discord.ui.button(label="🔊+", style=discord.ButtonStyle.success)
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        volumes[gid] = min(2.0, volumes.get(gid,1.0) + 0.1)
        await interaction.response.send_message(f"🔊 Głośność: {int(volumes[gid]*100)}%", ephemeral=True)
        info = current_song.get(gid)
        if info:
            suppress_after[gid] = True
            v = interaction.guild.voice_client
            if v: v.stop()
            await start_playback(self.ctx, info["url"], info["title"], info.get("webpage"), info.get("thumb"), info.get("duration"), start_offset=int(get_play_position(gid)), send_np=False)

    @discord.ui.button(label="🔈-", style=discord.ButtonStyle.danger)
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        volumes[gid] = max(0.05, volumes.get(gid,1.0) - 0.1)
        await interaction.response.send_message(f"🔉 Głośność: {int(volumes[gid]*100)}%", ephemeral=True)
        info = current_song.get(gid)
        if info:
            suppress_after[gid] = True
            v = interaction.guild.voice_client
            if v: v.stop()
            await start_playback(self.ctx, info["url"], info["title"], info.get("webpage"), info.get("thumb"), info.get("duration"), start_offset=int(get_play_position(gid)), send_np=False)

    @discord.ui.button(label="⏹️", style=discord.ButtonStyle.danger)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        queues[gid] = []
        filters.pop(gid, None)
        bass_gain.pop(gid, None)
        current_song.pop(gid, None)
        suppress_after[gid] = True
        v = interaction.guild.voice_client
        if v:
            v.stop(); await v.disconnect()
        await interaction.response.send_message("⏹ Zatrzymano i rozłączono.", ephemeral=False)

# -----------------------
# Now playing (embed + buttons)
# -----------------------
async def send_now_playing(ctx, gid):
    info = current_song.get(gid)
    if not info:
        return
    title = info["title"]
    pos = int(get_play_position(gid))
    dur = info.get("duration")
    if dur:
        frac = min(1.0, pos/dur) if dur>0 else 0
        filled = int(frac*20)
        prog = "[" + "█"*filled + "░"*(20-filled) + f"] {pos}/{dur}s"
    else:
        prog = f"{pos}s"
    embed = discord.Embed(title="Teraz grane", description=f"**{title}**\n{prog}", color=0x1DB954)
    if info.get("webpage"): embed.url = info.get("webpage")
    if info.get("thumb"): embed.set_thumbnail(url=info.get("thumb"))
    view = PlayerView(gid, ctx)
    msg = await ctx.send(embed=embed, view=view)
    control_message[gid] = msg.id

# -----------------------
# Commands
# -----------------------
@bot.command()
async def join(ctx):
    if ctx.author.voice:
        await ctx.author.voice.channel.connect(); await ctx.send("🔊 Dołączyłem!")
    else:
        await ctx.send("Musisz być na kanale głosowym!")

@bot.command()
async def leave(ctx):
    v = ctx.voice_client
    if v:
        await v.disconnect(); await ctx.send("👋 Opuszczam kanał.")
    else:
        await ctx.send("Nie jestem na kanale.")

@bot.command()
async def play(ctx, *, query):
    res = fetch_info(query)
    if not res:
        return await ctx.send("❌ Nie udało się pobrać utworu.")
    stream_url, title, webpage, thumb, dur = res
    gid = ctx.guild.id
    v = ctx.voice_client
    if v and v.is_playing():
        get_queue(gid).append((stream_url, title, webpage, thumb, dur))
        return await ctx.send(f"➕ Dodano do kolejki: **{title}**")
    await start_playback(ctx, stream_url, title, webpage, thumb, dur, start_offset=0.0, send_np=True)

@bot.command()
async def playlist(ctx, url):
    entries = fetch_playlist_entries(url)
    if not entries:
        return await ctx.send("❌ Nie udało się pobrać playlisty lub jest pusta.")
    gid = ctx.guild.id
    for ent in entries:
        get_queue(gid).append(ent)
    await ctx.send(f"➕ Dodano {len(entries)} utworów z playlisty do kolejki.")

@bot.command()
async def search(ctx, *, query):
    with youtube_dl.YoutubeDL(ydl_opts) as ydl:
        data = ydl.extract_info(f"ytsearch5:{query}", download=False)
    results = []
    if not data or "entries" not in data:
        return await ctx.send("❌ Nie znaleziono wyników.")
    for e in data["entries"]:
        if not e: continue
        title = e.get("title","Unknown")
        webpage = e.get("webpage_url")
        thumb = e.get("thumbnail")
        duration = e.get("duration")
        results.append((title, webpage, thumb, duration))
    if not results:
        return await ctx.send("❌ Nie znaleziono wyników.")
    search_results[ctx.guild.id] = results
    msg = "**Wyniki wyszukiwania (wybierz !select <nr>):**\n"
    for i, (title, webpage, thumb, duration) in enumerate(results, start=1):
        durtxt = f" [{duration}s]" if duration else ""
        msg += f"{i}. {title}{durtxt}\n"
    await ctx.send(msg)

@bot.command()
async def select(ctx, index: int):
    gid = ctx.guild.id
    if gid not in search_results:
        return await ctx.send("❌ Brak aktywnego wyszukiwania. Użyj !search <query>.")
    arr = search_results[gid]
    if not (1 <= index <= len(arr)):
        return await ctx.send("❌ Nieprawidłowy numer.")
    title, webpage, thumb, duration = arr[index-1]
    res = fetch_info(webpage or title)
    if not res:
        return await ctx.send("❌ Nie udało się pobrać wybranego utworu.")
    stream_url, title2, webpage2, thumb2, dur2 = res
    v = ctx.voice_client
    if v and v.is_playing():
        get_queue(gid).append((stream_url, title2, webpage2, thumb2, dur2))
        return await ctx.send(f"➕ Dodano do kolejki: **{title2}**")
    await start_playback(ctx, stream_url, title2, webpage2, thumb2, dur2, start_offset=0.0, send_np=True)

@bot.command()
async def skip(ctx):
    v = ctx.voice_client
    if not v or not v.is_playing():
        return await ctx.send("❌ Nic nie gra.")
    v.stop(); await ctx.send("⏭ Pominięto.")

@bot.command()
async def stop(ctx):
    gid = ctx.guild.id
    queues[gid] = []
    filters.pop(gid, None)
    bass_gain.pop(gid, None)
    current_song.pop(gid, None)
    v = ctx.voice_client
    if v:
        suppress_after[gid] = True; v.stop(); await v.disconnect()
    await ctx.send("🛑 Zatrzymano i rozłączono.")

@bot.command()
async def pause(ctx):
    v = ctx.voice_client
    if v and v.is_playing():
        v.pause(); await ctx.send("⏸ Wstrzymano.")
    else:
        await ctx.send("❌ Nic nie gra.")

@bot.command()
async def resume(ctx):
    v = ctx.voice_client
    if v and v.is_paused():
        v.resume(); await ctx.send("▶ Wznowiono.")
    else:
        await ctx.send("❌ Nie ma pauzy.")

# -----------------------
# Volume (1-200)
# -----------------------
@bot.command()
async def volume(ctx, vol: int = None):
    gid = ctx.guild.id
    if vol is None:
        current = int(volumes.get(gid, 1.0) * 100)
        return await ctx.send(f"🔊 Aktualna głośność: **{current}%**")
    if vol < 1 or vol > 200:
        return await ctx.send("🔈 Podaj wartość **1–200**.")
    volumes[gid] = vol / 100
    v = ctx.voice_client
    if v and getattr(v, "source", None) and isinstance(v.source, discord.PCMVolumeTransformer):
        v.source.volume = volumes[gid]
    # confirmation only (no embed refresh)
    await ctx.send(f"🔊 Ustawiono głośność na **{vol}%**.")

# -----------------------
# Seek
# -----------------------
def parse_time_string(t: str):
    try:
        parts = list(map(int, t.split(":")))
    except:
        return None
    if len(parts) == 1: return parts[0]
    if len(parts) == 2: return parts[0]*60 + parts[1]
    if len(parts) == 3: return parts[0]*3600 + parts[1]*60 + parts[2]
    return None

@bot.command()
async def seek(ctx, t: str):
    seconds = parse_time_string(t)
    if seconds is None:
        return await ctx.send("Podaj czas w sekundach lub mm:ss")
    gid = ctx.guild.id
    if gid not in current_song:
        return await ctx.send("❌ Nic nie gra.")
    stream_url = current_song[gid]["url"]
    title = current_song[gid]["title"]
    v = ctx.voice_client
    if not v:
        return await ctx.send("❌ Bot nie jest połączony.")
    suppress_after[gid] = True
    v.stop()
    await start_playback(ctx, stream_url, title, current_song[gid].get("webpage"), current_song[gid].get("thumb"), current_song[gid].get("duration"), start_offset=seconds, send_np=False)
    await ctx.send(f"⏩ Przewinięto do {t} ({seconds}s).")

# -----------------------
# Filters: ON/OFF logic, single main filter at once, bass independent
# -----------------------
async def apply_filter_immediate(ctx, gid):
    if gid not in current_song:
        return await ctx.send("❌ Nic nie gra.")
    pos = int(get_play_position(gid))
    stream_url = current_song[gid]["url"]
    title = current_song[gid]["title"]
    v = ctx.voice_client
    if not v:
        return await ctx.send("❌ Bot nie jest połączony.")
    suppress_after[gid] = True
    v.stop()
    # restart but DO NOT send embed (confirmation already sent by caller)
    await start_playback(ctx, stream_url, title, current_song[gid].get("webpage"), current_song[gid].get("thumb"), current_song[gid].get("duration"), start_offset=pos, send_np=False)

@bot.command()
async def bass(ctx, level: int = None):
    gid = ctx.guild.id
    if level is None:
        current = bass_gain.get(gid, 0)
        return await ctx.send(f"🎚️ Aktualny Bass Boost: **{current} dB**")
    if level < -20 or level > 20:
        return await ctx.send("🎚️ Bass boost: **-20 do +20 dB**.")
    bass_gain[gid] = level
    # apply immediately but only confirmation message shown
    await apply_filter_immediate(ctx, gid)
    await ctx.send(f"🎵 Zastosowano Bass Boost: **{level} dB**")

@bot.command()
async def nightcore(ctx):
    gid = ctx.guild.id
    cur = filters.get(gid)
    if cur == "nightcore":
        filters.pop(gid, None)
        # remove main filter -> apply (restart from pos)
        await apply_filter_immediate(ctx, gid)
        return await ctx.send("✨ Nightcore WYŁĄCZONY.")
    # enable nightcore, disable other main filters (vaporwave / eq)
    filters[gid] = "nightcore"
    await apply_filter_immediate(ctx, gid)
    await ctx.send("✨ Nightcore WŁĄCZONY.")

@bot.command()
async def vaporwave(ctx):
    gid = ctx.guild.id
    cur = filters.get(gid)
    if cur == "vaporwave":
        filters.pop(gid, None)
        await apply_filter_immediate(ctx, gid)
        return await ctx.send("🌫️ Vaporwave WYŁĄCZONY.")
    filters[gid] = "vaporwave"
    await apply_filter_immediate(ctx, gid)
    await ctx.send("🌫️ Vaporwave WŁĄCZONY.")

@bot.command()
async def resetfilter(ctx):
    gid = ctx.guild.id
    filters.pop(gid, None)
    bass_gain.pop(gid, None)
    await apply_filter_immediate(ctx, gid)
    await ctx.send("❌ Wyłączono wszystkie efekty.")

# -----------------------
# Queue commands
# -----------------------
@bot.command(name="queue")
async def queue_cmd(ctx):
    q = get_queue(ctx.guild.id)
    if not q: return await ctx.send("🟦 Kolejka pusta.")
    msg = "**🎵 Kolejka:**\n"
    for i, item in enumerate(q, 1):
        title = item[1] if len(item)>1 else item[0]
        msg += f"{i}. {title}\n"
    await ctx.send(msg)

@bot.command()
async def remove(ctx, idx: int):
    q = get_queue(ctx.guild.id)
    if not (1 <= idx <= len(q)): return await ctx.send("❌ Nieprawidłowy numer.")
    removed = q.pop(idx-1)
    await ctx.send(f"🗑 Usunięto **{removed[1]}**")

@bot.command()
async def clear(ctx):
    queues[ctx.guild.id] = []
    await ctx.send("🧹 Kolejka wyczyszczona.")

@bot.command()
async def shuffle(ctx):
    random.shuffle(get_queue(ctx.guild.id))
    await ctx.send("🔀 Kolejka wymieszana.")

@bot.command(name="songhistory")
async def songhistory(ctx):
    h = get_history(ctx.guild.id)
    if not h: return await ctx.send("Historia pusta.")
    await ctx.send("📜 Ostatnie utwory:\n" + "\n".join(h[-10:]))

# -----------------------
# Now playing & help
# -----------------------
@bot.command()
async def np(ctx):
    gid = ctx.guild.id
    info = current_song.get(gid)
    if not info:
        return await ctx.send("❌ Nic nie gra.")
    # wyświetl embed + przyciski bez restartu źródła
    await send_now_playing(ctx, gid)


@bot.command()
async def help(ctx):
    txt = """🎵 **LISTA KOMEND MUZYCZNYCH 3.1 (PCM SIMPLE)** 🎵

!join / !leave
!play <nazwa/link>
!playlist <yt_playlist_link>
!search <query> -> !select <nr>
!skip / !stop / !pause / !resume / !seek <mm:ss lub ss>
!queue / !remove <nr> / !clear / !shuffle / !songhistory / !np

Efekty (ON/OFF):
!bass <db> (independent)
!nightcore (toggle)
!vaporwave (toggle)
!resetfilter

Volume:
!volume <1-200>

Loop: !loop off|single|queue
Autoplay: !autoplay_cmd on/off
"""
    await ctx.send(txt)

# -----------------------
# Loop / Autoplay
# -----------------------
@bot.command()
async def loop(ctx, mode: str = None):
    gid = ctx.guild.id
    if mode is None:
        cur = loop_mode.get(gid,0); await ctx.send(f"🔁 Tryb loop: {cur} (0=off,1=single,2=queue)"); return
    m = mode.lower()
    if m in ("off","0"): loop_mode[gid] = 0
    elif m in ("single","1"): loop_mode[gid] = 1
    elif m in ("queue","2"): loop_mode[gid] = 2
    else: return await ctx.send("Użyj: off/single/queue")
    await ctx.send(f"🔁 Ustawiono loop: {loop_mode[gid]}")

@bot.command()
async def autoplay_cmd(ctx, mode: str):
    gid = ctx.guild.id
    if mode.lower() in ("on","true","1"): autoplay[gid] = True; await ctx.send("🔁 Autoplay włączony.")
    else: autoplay[gid] = False; await ctx.send("🔁 Autoplay wyłączony.")

# -----------------------
# On ready
# -----------------------
@bot.event
async def on_ready():
    print(f"Zalogowano jako {bot.user} (Music Bot v3.1 PCM SIMPLE)")

# -----------------------
# Run
# -----------------------
import os

TOKEN = os.getenv("DISCORD_TOKEN")
client.run(TOKEN)


