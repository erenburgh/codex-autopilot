"""Уведомление о готовности: единственный путь, который у нас есть.

Замерено на живом App Server, а не предположено:

- ``initialize`` не возвращает списка возможностей вовсе - ни одного
  объявленного API про непрочитанное, бейджи или уведомления;
- ``thread/metadata/update`` принимает только ``projectId``. Контрольный
  опыт: то же поле с прежним значением проходит, а ``name``, ``title``,
  ``threadName``, ``section``, ``sectionEnteredAt``, ``agentNickname`` и
  ``agentRole`` отвергаются одинаковым "must include at least one field";
- методов ``thread/rename``, ``thread/setName``, ``thread/title/update``,
  ``thread/markUnread``, ``thread/setUnread``, ``thread/unread/update``,
  ``thread/notify`` и ``notification/create`` не существует.

Значит ни отметить ветку прочитанной, ни переименовать её после
создания нельзя. Состояние "непрочитано" принадлежит интерфейсу Desktop,
и снаружи оно не наше.

Зато диспетчер - обычный локальный процесс на машине пользователя, и
системный банер ему доступен без чьего-либо API. Это и есть ответ на
настоящий вопрос: не "покажи бейдж", а "скажи, когда готово".

Выключено по умолчанию. Уведомление - побочный эффект на машине
человека, и включается оно явно.
"""

from __future__ import annotations

import shutil
import subprocess

# Текст уходит аргументами, а не внутрь скрипта: заголовки задач несут
# кавычки, скобки и кириллицу, и склейка строк тут рано или поздно
# превратилась бы в инъекцию или в синтаксическую ошибку AppleScript.
_SCRIPT = """on run argv
    display notification (item 3 of argv) with title (item 1 of argv) subtitle (item 2 of argv)
end run"""

_MAX_FIELD_CHARS = 200


def notify(cfg, title: str, subtitle: str, message: str) -> bool:
    """Показать системный банер. Никогда не бросает и ничего не ждёт.

    Возвращает True, только если банер действительно отправлен.
    Уведомление не вправе ни задержать пайплайн, ни уронить его: сбой
    здесь означает лишь то, что человек не увидел подсказки.
    """

    if not getattr(getattr(cfg, "runtime", None), "desktop_notifications", False):
        return False
    binary = shutil.which("osascript")
    if not binary:
        return False
    try:
        subprocess.run(
            [binary, "-", _clip(title), _clip(subtitle), _clip(message)],
            input=_SCRIPT,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return False
    return True


def _clip(value: str) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= _MAX_FIELD_CHARS:
        return text
    return text[: _MAX_FIELD_CHARS - 1] + "…"
