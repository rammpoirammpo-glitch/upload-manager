import re
from datetime import datetime

def sanitize_filename(filename: str) -> str:
    """
    Sanitizes a string to ensure it is safe for Windows and Unix filesystems.
    Removes invalid characters: < > : " / \\ | ? * and strips leading/trailing spaces/dots.
    """
    if not filename:
        return ""
    # Strip invalid filesystem characters
    sanitized = re.sub(r'[<>:"/\\|?*]', '_', filename)
    # Strip leading/trailing whitespaces and dots (dots at end can be problematic on Windows)
    sanitized = sanitized.strip().strip('.')
    return sanitized or "file"

def get_media_filename(message, prefix_date=None, default_ext=None) -> str:
    """
    Generates a clean, consistent filename for any Telegram message media.
    If prefix_date is True (or enabled in config), prepends 'YYYY-MM-DD_' based on message.date.
    
    Examples:
    - Photo with date: '2026-01-01_Photo_105.jpg'
    - Video with original name: '2026-01-01_my_clip.mp4'
    - Video without name: '2026-01-01_Video_106.mp4'
    - Document: '2026-01-01_report.pdf'
    """
    if prefix_date is None:
        try:
            from ui.views.settings_view import load_config
            cfg = load_config()
            prefix_date = cfg.get("prefix_file_date", True)
        except Exception:
            prefix_date = True

    msg_id = getattr(message, 'id', 0)
    
    # 1. Resolve Extension
    ext = ""
    if getattr(message, 'file', None) and getattr(message.file, 'ext', None):
        ext = message.file.ext or ""
    if not ext and default_ext:
        ext = default_ext

    # 2. Check Media Types
    is_photo = bool(getattr(message, 'photo', None))
    mime = ""
    if getattr(message, 'document', None):
        mime = getattr(message.document, 'mime_type', '') or ""
    elif getattr(message, 'file', None):
        mime = getattr(message.file, 'mime_type', '') or ""

    is_video = bool(getattr(message, 'video', None) or (mime and mime.startswith('video/')))
    is_audio = bool(getattr(message, 'audio', None) or getattr(message, 'voice', None) or (mime and mime.startswith('audio/')))

    # 3. Check for explicitly named file in Telegram metadata
    orig_name = None
    if getattr(message, 'file', None) and getattr(message.file, 'name', None):
        orig_name = message.file.name
    elif getattr(message, 'document', None) and getattr(message.document, 'attributes', None):
        try:
            from telethon.tl.types import DocumentAttributeFilename
            for attr in message.document.attributes:
                if isinstance(attr, DocumentAttributeFilename) and getattr(attr, 'file_name', None):
                    orig_name = attr.file_name
                    break
        except Exception:
            pass

    # 4. Determine Base Filename
    if orig_name:
        base_name = orig_name
    elif is_photo:
        base_name = f"Photo_{msg_id}{ext if ext else '.jpg'}"
    elif is_video:
        base_name = f"Video_{msg_id}{ext if ext else '.mp4'}"
    elif is_audio:
        base_name = f"Audio_{msg_id}{ext if ext else '.mp3'}"
    elif mime == 'application/pdf':
        base_name = f"Document_{msg_id}{ext if ext else '.pdf'}"
    elif mime in ['application/zip', 'application/x-rar-compressed', 'application/x-7z-compressed']:
        base_name = f"Document_{msg_id}{ext if ext else '.zip'}"
    else:
        base_name = f"Document_{msg_id}{ext}"

    base_name = sanitize_filename(base_name)

    # 5. Extract Publication Date
    date_prefix = ""
    if prefix_date:
        msg_date = getattr(message, 'date', None)
        if msg_date:
            if isinstance(msg_date, str):
                try:
                    msg_date = datetime.fromisoformat(msg_date)
                except Exception:
                    if len(msg_date) >= 10 and msg_date[4] == '-' and msg_date[7] == '-':
                        date_prefix = msg_date[:10]
            if hasattr(msg_date, 'strftime'):
                date_prefix = msg_date.strftime("%Y-%m-%d")

    # 6. Prepend Date Prefix if available and not already present
    if date_prefix and not base_name.startswith(date_prefix):
        return f"{date_prefix}_{base_name}"

    return base_name
