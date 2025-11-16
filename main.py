import logging
import os
import asyncio
from functools import wraps
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters,
    PicklePersistence  # <<< CORREÇÃO DE PERSISTÊNCIA (1/2): Importa
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

# Bibliotecas de API
import pylast
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

# --- 1. CONFIGURAÇÃO (LENDO TODAS AS 5 CHAVES DO AMBIENTE) ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY")
LASTFM_API_SECRET = os.getenv("LASTFM_API_SECRET")
SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")

# Períodos válidos
VALID_PERIODS = ['7day', '1month', '3month', '6month', '12month', 'overall']
DEFAULT_PERIOD = '7day'
BR_TIMEZONE = ZoneInfo("America/Sao_Paulo")

# Configuração de logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


# --- 2. VERIFICAÇÃO DE INICIALIZAÇÃO ---

if not all([TELEGRAM_TOKEN, LASTFM_API_KEY, LASTFM_API_SECRET, SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET]):
    logger.critical("=" * 50)
    logger.critical("ERRO: Variáveis de ambiente incompletas!")
    logger.critical("Verifique se as 5 chaves estão configuradas.")
    logger.critical("=" * 50)
    exit(1)


# --- 3. INICIALIZAÇÃO DAS APIs ---

# Last.fm
try:
    network = pylast.LastFMNetwork(
        api_key=LASTFM_API_KEY, api_secret=LASTFM_API_SECRET
    )
    logger.info("Conectado ao Last.fm com sucesso.")
except Exception as e:
    logger.critical(f"Falha CRÍTICA ao conectar no Last.fm: {e}")
    exit(1)

# Spotify
try:
    auth_manager = SpotifyClientCredentials(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET
    )
    sp = spotipy.Spotify(auth_manager=auth_manager)
    sp.search(q="test", type="track", limit=1)
    logger.info("Conectado ao Spotify com sucesso.")
except Exception as e:
    logger.critical(f"Falha CRÍTICA ao conectar no Spotify: {e}")
    exit(1)


# --- 4. DECORADOR DE ERROS ---

def handle_lastfm_errors(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        try:
            return await func(update, context, *args, **kwargs)
        except pylast.WSError as e:
            error_message = str(e).lower()
            if "user not found" in error_message:
                username, _ = _get_user_and_period(context)
                if not username: username = context.user_data.get('lastfm_user', 'usuário')
                await update.message.reply_text(f"❌ Não encontrei o usuário '{username}' no Last.fm.")
            elif "artist not found" in error_message:
                artist_name = " ".join(context.args)
                await update.message.reply_text(f"❌ Não encontrei o artista '{artist_name}'.")
            elif "album not found" in error_message or "track not found" in error_message:
                query = " ".join(context.args)
                await update.message.reply_text(f"❌ Não encontrei: '{query}'.\nLembre-se do formato: `Artista - Item`")
            else:
                logger.error(f"Erro de API no comando /{func.__name__}: {e}")
                await update.message.reply_text(f"Ocorreu um erro no Last.fm: {e}")
        except Exception as e:
            logger.error(f"Erro inesperado no comando /{func.__name__}: {e}")
            await update.message.reply_text("Ocorreu um erro inesperado.")
    return wrapper


# --- 5. FUNÇÕES DE AJUDA (Helpers) ---

def _get_user_and_period(context: ContextTypes.DEFAULT_TYPE) -> (str, str):
    """Busca nome de usuário e período a partir dos argumentos."""
    username = context.user_data.get('lastfm_user')
    period = DEFAULT_PERIOD
    args = list(context.args)
    if args:
        if args[-1].lower() in VALID_PERIODS:
            period = args.pop().lower()
        if args:
            username = " ".join(args)
    return username, period

def _parse_artist_item_query(context: ContextTypes.DEFAULT_TYPE) -> (str, str):
    """Processa uma query no formato "Artista - Item"."""
    query = " ".join(context.args)
    if ' - ' not in query:
        return None, None
    artist, item = query.split(' - ', 1)
    return artist.strip(), item.strip()

async def _send_with_photo_or_text(update: Update, image_url: str, caption: str):
    """Envia foto com legenda. Faz fallback para texto."""
    TEXT_LIMIT = 4096
    if image_url:
        try:
            await update.message.reply_photo(
                photo=image_url,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN
            )
            return
        except TelegramError as e:
            logger.warning(f"Falha ao enviar foto (legenda longa?): {e}. Usando fallback de texto.")
    
    try:
        await update.message.reply_text(caption, parse_mode=ParseMode.MARKDOWN)
    except TelegramError as e:
        if "message is too long" in str(e).lower():
            logger.warning(f"Fallback de texto falhou (msg > 4096). Truncando.")
            truncated_caption = caption[:(TEXT_LIMIT - 25)] + "\n\n... [MENSAGEM TRUNCADA]"
            await update.message.reply_text(truncated_caption, parse_mode=ParseMode.MARKDOWN)
        else:
            logger.error(f"Erro inesperado no fallback de texto: {e}")
            await update.message.reply_text("Ocorreu um erro ao formatar esta resposta.")

def _get_group_lastfm_users(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """
    Retorna o dicionário de usuários Last.fm inscritos no chat.
    Formato: {lastfm_username: telegram_user_id, ...}
    """
    if 'lastfm_users' not in context.chat_data:
        context.chat_data['lastfm_users'] = {}
    return context.chat_data['lastfm_users']

# --- 6. NOVOS HELPERS DE IMAGEM (Spotify + Fallback) ---

async def _get_spotify_image_url(artist_name: str, item_name: str, item_type: str = 'track') -> str | None:
    
    def blocking_spotify_search():
        """Função síncrona que faz a busca (será rodada em uma thread)."""
        try:
            query = f'artist:"{artist_name}" {item_type}:"{item_name}"'
            
            if item_type == 'track':
                results = sp.search(q=query, type='track', limit=1)
                if results['tracks']['items']:
                    return results['tracks']['items'][0]['album']['images'][0]['url']
            elif item_type == 'album':
                results = sp.search(q=query, type='album', limit=1)
                if results['albums']['items']:
                    return results['albums']['items'][0]['images'][0]['url']
            elif item_type == 'artist':
                query = f'artist:"{artist_name}"'
                results = sp.search(q=query, type='artist', limit=1)
                if results['artists']['items']:
                    return results['artists']['items'][0]['images'][0]['url']
        except Exception as e:
            logger.error(f"Erro na função blocking_spotify_search: {e}")
            return None
        return None

    try:
        image_url = await asyncio.to_thread(blocking_spotify_search)
        if image_url:
            logger.info(f"Spotify ENCONTROU imagem para: {artist_name} - {item_name}")
            return image_url
        else:
            logger.warning(f"Spotify NÃO encontrou imagem para: {artist_name} - {item_name}")
            return None
    except Exception as e:
        logger.error(f"Erro ao rodar asyncio.to_thread para Spotify: {e}")
        return None

def _get_lastfm_image_fallback(pylast_item, item_type: str = 'album') -> str | None:
    """Função de fallback que busca a melhor imagem no Last.fm."""
    logger.info(f"Usando fallback do Last.fm para {pylast_item.name}...")
    
    if item_type == 'artist':
        image_getter = pylast_item.get_image
    else:
        image_getter = pylast_item.get_cover_image

    try: return image_getter(pylast.SIZE_MEGA)
    except Exception:
        try: return image_getter(pylast.SIZE_EXTRALARGE)
        except Exception:
            try: return image_getter(pylast.SIZE_LARGE)
            except Exception:
                logger.error(f"Fallback do Last.fm falhou para {pylast_item.name}")
                return None


# --- 7. COMANDOS DO BOT (COM CORREÇÕES DE NOME/FUSO) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envia a mensagem de boas-vindas."""
    user = update.effective_user
    await update.message.reply_html(
        f"Olá, {user.mention_html()}! 👋\n\n"
        "Eu sou seu bot de Last.fm.\n"
        "Para começar, salve seu nome de usuário com:\n"
        "`/set seu_usuario_lastfm`\n\n"
        "Seus dados agora ficam salvos mesmo se eu reiniciar! Use `/help` para ver os comandos."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra a lista de comandos."""
    help_text = (
        "ℹ️ *Lista de Comandos Disponíveis* ℹ️\n\n"
        "*Geral:*\n"
        "/start, /help, /set `[usuario]`\n\n"
        "*Scrobbles:*\n"
        "/np \n"
        "/recent \n\n"
        "*Comandos 'Top' (Período opcional):*\n"
        "Períodos: `7day`, `1month`, `3month`, `6month`, `12month`, `overall`\n"
        "Ex: `/topartists 1month `\n"
        "/topartists `[periodo] `\n"
        "/topalbums `[periodo] `\n"
        "/toptracks `[periodo] `\n\n"
        "*Informações:*\n"
        "Use `Artista - Item` para buscar.\n"
        "/artist `[nome do artista]`\n"
        "/album `[artista] - [nome do album]`\n"
        "/track `[artista] - [nome da musica]`\n"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def set_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Salva o nome de usuário (rápido, sem verificação)."""
    if not context.args:
        await update.message.reply_text("Exemplo: `/set RjDj`", parse_mode=ParseMode.MARKDOWN)
        return
    username = " ".join(context.args)
    context.user_data['lastfm_user'] = username
    await update.message.reply_text(f"✅ Usuário Last.fm salvo como: {username}\nSeus dados estão persistidos!")


@handle_lastfm_errors
async def now_playing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra o Now Playing (Com lógica de nome de usuário)"""
    
    lastfm_user, _ = _get_user_and_period(context)
    if not lastfm_user:
        await update.message.reply_text("Use `/set [usuario]` primeiro ou digite `/np [usuario]`.", parse_mode=ParseMode.MARKDOWN)
        return

    args_without_period = list(context.args)
    if args_without_period and args_without_period[-1].lower() in VALID_PERIODS:
        args_without_period.pop()
    
    if not args_without_period: 
        display_name = update.effective_user.first_name
    else: 
        display_name = lastfm_user

    user = network.get_user(lastfm_user)
    now_playing = user.get_now_playing()

    if now_playing is None:
        await update.message.reply_text(f"🎧 *{display_name}* não está ouvindo nada no momento.", parse_mode=ParseMode.MARKDOWN)
        return

    scrobble_list = user.get_track_scrobbles(
        now_playing.artist.name, now_playing.title
    )
    scrobble_count = len(scrobble_list)
    artist = now_playing.artist
    album = now_playing.get_album()
        
    image_url = await _get_spotify_image_url(
        artist.name, now_playing.title, 'track')
        
    message_text = (
        f"🎧 *{display_name}* está ouvindo:\n\n"
        f"🎵 *Música:* {now_playing.title}\n"
        f"🎤 *Artista:* {artist.name}\n")
    
    if album:
        message_text += f"💿 *Álbum:* {album.get_title()}\n"
        if not image_url:
            image_url = _get_lastfm_image_fallback(album, 'album')

    message_text += f"📈 *Scrobbles:* {scrobble_count}"

    await _send_with_photo_or_text(update, image_url, message_text)

@handle_lastfm_errors
async def recent_tracks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra as 10 últimas músicas ouvidas (Com fuso e nome corrigidos)"""
    
    lastfm_user, _ = _get_user_and_period(context)
    if not lastfm_user:
        await update.message.reply_text("Use `/set [usuario]` primeiro.", parse_mode=ParseMode.MARKDOWN)
        return
        
    args_without_period = list(context.args)
    if args_without_period and args_without_period[-1].lower() in VALID_PERIODS:
        args_without_period.pop()
    
    if not args_without_period:
        display_name = update.effective_user.first_name
    else:
        display_name = lastfm_user
        
    user = network.get_user(lastfm_user)
    recent_tracks = user.get_recent_tracks(limit=10)
        
    if not recent_tracks:
        await update.message.reply_text(f"*{display_name}* não ouviu nenhuma música.", parse_mode=ParseMode.MARKDOWN)
        return

    message_lines = [f"📄 *Últimas 10 músicas de {display_name}:*\n"]
    for track in recent_tracks:
        
        utc_dt = datetime.fromtimestamp(int(track.timestamp), tz=ZoneInfo("UTC"))
        brt_dt = utc_dt.astimezone(BR_TIMEZONE)
        playback_time = brt_dt.strftime('%d/%m %H:%M')

        message_lines.append(
            f"• `{playback_time}`: *{track.track.artist.name}* - {track.track.title}"
        )
    await update.message.reply_text("\n".join(message_lines), parse_mode=ParseMode.MARKDOWN)


@handle_lastfm_errors
async def top_artists(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra os top artistas (Com lógica de nome de usuário)"""
    
    lastfm_user, period = _get_user_and_period(context)
    if not lastfm_user:
        await update.message.reply_text("Use `/set [usuario]` primeiro.", parse_mode=ParseMode.MARKDOWN)
        return
  
    args_without_period = list(context.args)
    if args_without_period and args_without_period[-1].lower() in VALID_PERIODS:
        args_without_period.pop()
    
    if not args_without_period:
        display_name = update.effective_user.first_name
    else:
        display_name = lastfm_user
  
    user = network.get_user(lastfm_user)
    top_items = user.get_top_artists(period=period, limit=10)

    if not top_items:
        await update.message.reply_text(f"*{display_name}* não tem artistas top no período '{period}'.", parse_mode=ParseMode.MARKDOWN)
        return

    message_lines = [f"🏆 *Top 10 Artistas de {display_name}* ({period}):\n"]
    for i, item in enumerate(top_items):
        message_lines.append(
            f"*{i+1}.* {item.item.name} `({item.weight} scrobbles)`"
        )
    await update.message.reply_text("\n".join(message_lines), parse_mode=ParseMode.MARKDOWN)
  
@handle_lastfm_errors
async def top_albums(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra os top álbuns (Com lógica de nome de usuário)"""
    
    lastfm_user, period = _get_user_and_period(context)
    if not lastfm_user:
        await update.message.reply_text("Use `/set [usuario]` primeiro.", parse_mode=ParseMode.MARKDOWN)
        return
  
    args_without_period = list(context.args)
    if args_without_period and args_without_period[-1].lower() in VALID_PERIODS:
        args_without_period.pop()
    
    if not args_without_period:
        display_name = update.effective_user.first_name
    else:
        display_name = lastfm_user
  
    user = network.get_user(lastfm_user)
    top_items = user.get_top_albums(period=period, limit=10)

    if not top_items:
        await update.message.reply_text(f"*{display_name}* não tem álbuns top no período '{period}'.", parse_mode=ParseMode.MARKDOWN)
        return

    message_lines = [f"📀 *Top 10 Álbuns de {display_name}* ({period}):\n"]
    for i, item in enumerate(top_items):
        message_lines.append(
            f"*{i+1}.* {item.item.artist.name} - *{item.item.title}* `({item.weight} scrobbles)`"
        )
    await update.message.reply_text("\n".join(message_lines), parse_mode=ParseMode.MARKDOWN)
  
@handle_lastfm_errors
async def top_tracks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra as top músicas (Com lógica de nome de usuário)"""
    
    lastfm_user, period = _get_user_and_period(context)
    if not lastfm_user:
        await update.message.reply_text("Use `/set [usuario]` primeiro.", parse_mode=ParseMode.MARKDOWN)
        return
  
    args_without_period = list(context.args)
    if args_without_period and args_without_period[-1].lower() in VALID_PERIODS:
        args_without_period.pop()
    
    if not args_without_period:
        display_name = update.effective_user.first_name
    else:
        display_name = lastfm_user
  
    user = network.get_user(lastfm_user)
    top_items = user.get_top_tracks(period=period, limit=10)

    if not top_items:
        await update.message.reply_text(f"*{display_name}* não tem músicas top no período '{period}'.", parse_mode=ParseMode.MARKDOWN)
        return

    message_lines = [f"🎵 *Top 10 Músicas de {display_name}* ({period}):\n"]
    for i, item in enumerate(top_items):
        message_lines.append(
            f"*{i+1}.* {item.item.artist.name} - *{item.item.title}* `({item.weight} scrobbles)`"
        )
    await update.message.reply_text("\n".join(message_lines), parse_mode=ParseMode.MARKDOWN)
  

@handle_lastfm_errors
async def artist_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Busca infos de artista (Lógica de Imagem Atualizada e scrobbles do usuário)"""
    
    # 1. Obter o nome do artista
    if not context.args:
        await update.message.reply_text("Formato: `/artist [nome do artista]`", parse_mode=ParseMode.MARKDOWN)
        return
    artist_name = " ".join(context.args)
    
    # 2. Obter o usuário Last.fm salvo
    lastfm_user = context.user_data.get('lastfm_user')
    if not lastfm_user:
        await update.message.reply_text("Use `/set [usuario]` primeiro para ver seus scrobbles do artista.", parse_mode=ParseMode.MARKDOWN)
        return
      
    user = network.get_user(lastfm_user)
    artist = network.get_artist(artist_name)
    artist.get_bio_summary()

    try:
        top_items = user.get_top_artists(limit=50, period='overall')
        
        for item in top_items:
            if item.item.name.lower() == artist.name.lower():
                user_playcount = int(item.weight) 
                break
                
    except Exception as e:
        logger.error(f"Falha ao buscar Top Artistas filtrado para o usuário: {e}.")
        
    if user_playcount is None:
        user_playcount = 0
        
    # Lógica de Imagem (Mantida)
    image_url = await _get_spotify_image_url(artist.name, "", 'artist')
    if not image_url:
        image_url = _get_lastfm_image_fallback(artist, 'artist')
        
    # 4. Formatação da mensagem
    scrobbles = "{:,}".format(user_playcount).replace(",", ".") 
    tags = [tag.item.name for tag in artist.get_top_tags(limit=5)]
    tags_str = ", ".join(tags) if tags else "Nenhuma tag encontrada"

    message_text = (
        f"🎤 *{artist.name}*\n\n"
        f"👤 *Scrobbles:* {scrobbles}\n"
        f"🏷️ *Tags:* {tags_str}\n")
    
    await _send_with_photo_or_text(update, image_url, message_text)
  
@handle_lastfm_errors
async def album_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Busca infos de álbum (Lógica de Imagem Atualizada)"""
    artist_name, album_name = _parse_artist_item_query(context)
    if not artist_name:
        await update.message.reply_text("Formato: `/album [artista] - [nome do album]`", parse_mode=ParseMode.MARKDOWN)
        return
          
    album = network.get_album(artist_name, album_name)
    album.get_playcount()

    image_url = await _get_spotify_image_url(album.artist.name, album.title, 'album')
    if not image_url:
        image_url = _get_lastfm_image_fallback(album, 'album')

    playcount = f"{album.get_playcount():,}"
    message_text = (
        f"💿 *{album.title}*\n"
        f"🎤 *Artista:* {album.artist.name}\n\n"
        f"📈 *Scrobbles:* {playcount}\n"
    )

    await _send_with_photo_or_text(update, image_url, message_text)
  
  
@handle_lastfm_errors
async def track_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Busca infos de música (Lógica de Imagem Atualizada)"""
    artist_name, track_name = _parse_artist_item_query(context)
    if not artist_name:
        await update.message.reply_text("Formato: `/track [artista] - [nome da musica]`", parse_mode=ParseMode.MARKDOWN)
        return
          
    track = network.get_track(artist_name, track_name)
    track.get_playcount()
    
    image_url = await _get_spotify_image_url(track.artist.name, track.title, 'track')

    playcount = f"{track.get_playcount():,}"
    listeners = f"{track.get_listener_count():,}"
    message_text = (
        f"🎵 *{track.title}*\n"
        f"🎤 *Artista:* {track.artist.name}\n\n"
        f"📈 *Scrobbles (total):* {playcount}\n"
        f"👥 *Ouvintes (total):* {listeners}\n")
        
    if not image_url:
        try:
            album = track.get_album()
            if album:
                message_text += f"💿 *Álbum (Last.fm):* {album.title}\n"
                image_url = _get_lastfm_image_fallback(album, 'album')
        except pylast.WSError:
            pass
            
    await _send_with_photo_or_text(update, image_url, message_text)

async def join_lastfm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Permite que o usuário se inscreva no Now Listening do grupo."""

    # 1. Obter o Last.fm username que o usuário salvou em /set
    lastfm_user = context.user_data.get('lastfm_user')
    if not lastfm_user:
        await update.message.reply_text(
            "Você precisa primeiro salvar seu usuário Last.fm com `/set seu_usuario` para participar do /nl.", 
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # 2. Obter a lista de usuários Last.fm do chat
    group_users = _get_group_lastfm_users(context)

    telegram_user_id = update.effective_user.id

    # 3. Mapear (telegram_id -> lastfm_user) para rastrear o usuário no grupo
    group_users[telegram_user_id] = {
        'lastfm_user': lastfm_user,
        'first_name': update.effective_user.first_name,
        'username': update.effective_user.username
    }
    user_display = update.effective_user.username
    if user_display:
        user_display = f"{user_display}"
    else:
        user_display = update.effective_user.first_name

    await update.message.reply_text(
        f"✅ Usuário *{user_display}* adicionado à lista /nl deste chat!", 
        parse_mode=ParseMode.MARKDOWN
    )

@handle_lastfm_errors
async def now_listening(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra o Now Playing de todos os usuários inscritos no chat, usando o nome do Telegram."""
    
    group_users = _get_group_lastfm_users(context)
    
    if not group_users:
        await update.message.reply_text(
            "Nenhum usuário se inscreveu ainda para o /nl. Use `/joinfm` para participar!", 
            parse_mode=ParseMode.MARKDOWN
        )
        return

    nl_message_lines = ["🎧 *Now Listening* do Grupo:"]
    listening_count = 0
    
    for user_info in group_users.values():
        lastfm_user = user_info['lastfm_user']
        
        # DEFININDO O NOME DE EXIBIÇÃO: Prioriza First Name + (@Username se existir)
        telegram_name = user_info['first_name']
        telegram_username = user_info.get('username')
        
        if telegram_username:
            # Formato: First Name (@Username)
            telegram_display = f"*{telegram_name}* (@{telegram_username})"
        else:
            # Formato: Apenas First Name (se não tiver username)
            telegram_display = f"*{telegram_name}*"

        try:
            user = network.get_user(lastfm_user)
            now_playing = await asyncio.to_thread(user.get_now_playing)

            if now_playing:
                listening_count += 1
                
                # Exibe o nome no formato solicitado (com indentação de 3 espaços)
                nl_message_lines.append(
                    f"\n• {telegram_display}:\n"
                    f"   {now_playing.title} - *{now_playing.artist.name}*"
                )
            
        except pylast.WSError as e:
            if "user not found" in str(e).lower():
                 nl_message_lines.append(f"\n• {telegram_display}: ❌ Usuário Last.fm não encontrado.")
            else:
                 logger.error(f"Erro ao buscar NP para {lastfm_user}: {e}")
        except Exception as e:
            logger.error(f"Erro inesperado no /nl: {e}")
            
    if listening_count == 0 and len(group_users) > 0:
        nl_message_lines.append("\n_Nenhum dos usuários inscritos está ouvindo algo no momento._")
        
    await update.message.reply_text("\n".join(nl_message_lines), parse_mode=ParseMode.MARKDOWN)

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde a comandos não reconhecidos."""
    await update.message.reply_text("Desculpe, não entendi. Use /help.")

async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde a mensagens de texto que não são comandos."""
    await update.message.reply_text("Eu só respondo a comandos. Use /help.")
  

# --- 8. FUNÇÃO PRINCIPAL (MAIN) ---
  
def main():
    """Inicia o bot e registra todos os comandos."""

    persistence = PicklePersistence(filepath='bot_persistence.pickle')

    application = Application.builder().token(TELEGRAM_TOKEN)\
        .persistence(persistence)\
        .build()
  
    # Registra os comandos (Handlers)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("set", set_username))
    application.add_handler(CommandHandler("np", now_playing))
    application.add_handler(CommandHandler("recent", recent_tracks))
    application.add_handler(CommandHandler("topartists", top_artists))
    application.add_handler(CommandHandler("topalbums", top_albums))
    application.add_handler(CommandHandler("toptracks", top_tracks))
    application.add_handler(CommandHandler("artist", artist_info))
    application.add_handler(CommandHandler("album", album_info))
    application.add_handler(CommandHandler("track", track_info))
    application.add_handler(CommandHandler("np", now_playing))
    application.add_handler(CommandHandler("recent", recent_tracks))
    application.add_handler(CommandHandler("joinfm", join_lastfm))
    application.add_handler(CommandHandler("nl", now_listening))
    
    # Handlers para mensagens desconhecidas
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
 
    logger.info("Iniciando o bot (com Spotify, correções e PERSISTÊNCIA)...")
    application.run_polling()
  
  
if __name__ == "__main__":
    main()