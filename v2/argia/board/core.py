"""Pure rules for the Solar team board (v325). No Flask, no database.

The board is a Kanban board run with a light Scrum rhythm: a backlog,
the work committed for this week, work in progress with a per-person
limit, a review step, done. A blocked card stays in its column with a
red flag and a written reason, which is what the stand-up talks about
first.
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, Iterable, List, Optional, Set

# (key, English, Spanish, help EN, help ES)
COLUMNS = [
    ('backlog', 'Backlog', 'Pendientes',
     'Everything we may do. Not committed yet.', 'Todo lo que podríamos hacer. Aún sin compromiso.'),
    ('todo', 'This week', 'Esta semana',
     'Committed for this week at the Monday review.', 'Comprometido para esta semana en la revisión del lunes.'),
    ('doing', 'In progress', 'En curso',
     'Someone works on it now. Limit: 3 cards per person.', 'Alguien trabaja en esto ahora. Límite: 3 tarjetas por persona.'),
    ('review', 'Review', 'Revisión',
     'Done by the owner, waiting for a second pair of eyes or the client.', 'Terminado por el responsable, esperando revisión o al cliente.'),
    ('done', 'Done', 'Hecho',
     'Finished and accepted. Hidden from the board after 14 days.', 'Terminado y aceptado. Se oculta del tablero después de 14 días.'),
]
COL_KEYS = [c[0] for c in COLUMNS]
WIP_LIMIT = 3            # cards in progress per person
DONE_WINDOW_DAYS = 14    # done cards stay visible this long

# The process map v0.1 (13 Oct 2026): every card can say which process it improves.
PROCESSES = [
    ('P1', 'Lead & qualification', 'Prospección y calificación'),
    ('P2', 'Design & offer', 'Diseño y oferta'),
    ('P3', 'Contract & handover', 'Contrato y entrega a proyectos'),
    ('P4', 'Engineering & permitting', 'Ingeniería y permisos'),
    ('P5', 'Procurement', 'Compras'),
    ('P6', 'Construction & installation', 'Construcción e instalación'),
    ('P7', 'Commissioning & interconnection', 'Puesta en marcha e interconexión'),
    ('P8', 'Operate & maintain', 'Operación y mantenimiento'),
    ('P9', 'Bill & collect', 'Facturación y cobranza'),
    ('M1', 'Performance & cadence', 'Desempeño y cadencia'),
    ('E1', 'Standards (AGS)', 'Estándares (AGS)'),
    ('E2', 'Tools & data', 'Herramientas y datos'),
    ('E3', 'Suppliers & contractors', 'Proveedores y contratistas'),
    ('E4', 'People & capability', 'Personas y capacidades'),
    ('E5', 'HSE & quality', 'Seguridad y calidad'),
]
PROCESS_KEYS = [p[0] for p in PROCESSES]
PRIORITIES = ('high', 'normal', 'low')
MEMBER_ROLES = ('editor', 'viewer')

MAX_TITLE = 200
MAX_DESC = 8000
MAX_COMMENT = 4000
MAX_REASON = 500
MAX_ORDER = 500

# what each role may do; 'owner' is everything
_RIGHTS = {
    'viewer': {'read', 'comment', 'watch'},
    'editor': {'read', 'comment', 'watch', 'create', 'edit', 'move', 'archive'},
    'owner': {'read', 'comment', 'watch', 'create', 'edit', 'move', 'archive', 'members', 'seed'},
}

EDITABLE = ('title', 'description', 'assignee', 'process', 'priority', 'due', 'blocked', 'blocked_reason')


class Invalid(ValueError):
    """A request the board refuses, with a message a person can act on."""


def can(role: Optional[str], action: str) -> bool:
    return action in _RIGHTS.get(role or '', set())


def clean_text(value, maxlen: int) -> str:
    """Text as stored: no NUL bytes (PostgreSQL refuses them), unix newlines,
    trimmed, cut at ``maxlen``."""
    s = '' if value is None else str(value)
    s = s.replace(chr(0), '').replace('\r\n', '\n').replace('\r', '\n').strip()
    return s[:maxlen]


def parse_due(value) -> Optional[str]:
    """'' / None -> None; 'YYYY-MM-DD' -> the same string; anything else is refused."""
    s = clean_text(value, 20)
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s).isoformat()
    except ValueError:
        raise Invalid(f'Due date must be YYYY-MM-DD, got "{s}".')


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def validate(data: Dict, partial: bool = False, people: Optional[Iterable[str]] = None) -> Dict:
    """Clean the editable fields of a card.

    ``partial`` = an edit: only the keys present are checked and returned.
    ``people``  = the usernames an assignee may be (None = do not check).
    A blocked card must say why; unblocking clears the reason."""
    if not isinstance(data, dict):
        raise Invalid('Expected an object.')
    out: Dict = {}
    keys = [k for k in EDITABLE if k in data] if partial else list(EDITABLE)
    for k in keys:
        v = data.get(k)
        if k == 'title':
            v = clean_text(v, MAX_TITLE)
            if not v:
                raise Invalid('A card needs a title.')
        elif k == 'description':
            v = clean_text(v, MAX_DESC)
        elif k == 'assignee':
            v = clean_text(v, 80)
            if v and people is not None and v not in set(people):
                raise Invalid(f'Unknown person "{v}".')
        elif k == 'process':
            v = clean_text(v, 8).upper()
            if v and v not in PROCESS_KEYS:
                raise Invalid(f'Unknown process "{v}".')
        elif k == 'priority':
            v = clean_text(v, 10).lower() or 'normal'
            if v not in PRIORITIES:
                raise Invalid(f'Priority must be one of {", ".join(PRIORITIES)}.')
        elif k == 'due':
            v = parse_due(v)
        elif k == 'blocked':
            v = _bool(v)
        elif k == 'blocked_reason':
            v = clean_text(v, MAX_REASON)
        out[k] = v
    if 'blocked' in out:
        if out['blocked'] and not out.get('blocked_reason', data.get('blocked_reason')):
            raise Invalid('Say why the card is blocked.')
        if not out['blocked']:
            out['blocked_reason'] = ''
    return out


def validate_col(col) -> str:
    c = clean_text(col, 20).lower()
    if c not in COL_KEYS:
        raise Invalid(f'Unknown column "{c}".')
    return c


def validate_order(order, card_id: int) -> List[int]:
    """The ids of the destination column, top to bottom, after the drop.
    Must be unique integers and include the moved card."""
    if not isinstance(order, list) or len(order) > MAX_ORDER:
        raise Invalid('Order must be a list of card ids.')
    out: List[int] = []
    for x in order:
        try:
            i = int(x)
        except (TypeError, ValueError):
            raise Invalid('Order must be a list of card ids.')
        if isinstance(x, bool) or i <= 0:
            raise Invalid('Order must be a list of card ids.')
        if i in out:
            raise Invalid('A card appears twice in the order.')
        out.append(i)
    if card_id not in out:
        raise Invalid('The order does not include the moved card.')
    return out


def card_number(card_id) -> str:
    return f'SB-{int(card_id):04d}'


def parse_ts(s) -> Optional[dt.datetime]:
    if not s:
        return None
    if isinstance(s, dt.datetime):
        return s if s.tzinfo else s.replace(tzinfo=dt.timezone.utc)
    txt = str(s).strip().replace(' ', 'T', 1)
    if txt.endswith('+00'):
        txt += ':00'
    try:
        d = dt.datetime.fromisoformat(txt)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def days_in_column(col_since, now: dt.datetime) -> int:
    d = parse_ts(col_since)
    if d is None:
        return 0
    return max(0, (now - d).days)


def aging(col: str, days: int) -> str:
    """'' / 'amber' / 'red' - how long a card has sat where it is. Only the
    columns where waiting is a problem get a colour."""
    limits = {'doing': (5, 10), 'review': (3, 7), 'todo': (7, 14)}
    if col not in limits:
        return ''
    amber, red = limits[col]
    return 'red' if days >= red else 'amber' if days >= amber else ''


def wip(cards: Iterable[Dict]) -> Dict[str, int]:
    """In-progress cards per assignee ('' = nobody)."""
    out: Dict[str, int] = {}
    for c in cards:
        if c.get('col') == 'doing' and not c.get('archived'):
            a = c.get('assignee') or ''
            out[a] = out.get(a, 0) + 1
    return out


def over_wip(cards: Iterable[Dict], limit: int = WIP_LIMIT) -> Set[str]:
    return {a for a, n in wip(cards).items() if a and n > limit}


def overdue(due, today: dt.date) -> bool:
    if not due:
        return False
    try:
        return dt.date.fromisoformat(str(due)[:10]) < today
    except ValueError:
        return False


# The first backlog: the "First 30 days" of the process map v0.1. Loaded once,
# by an owner, into an empty board (the Load starter backlog button).
STARTER = [
    dict(title='Review the process map with the Solar Director and CEO; agree one owner per process',
         process='M1', priority='high', col='todo'),
    dict(title='Define the G2 handover pack (Sales -> PM -> Solar Director)', process='P3', priority='high', col='todo'),
    dict(title='Time-box the Priority 1 engine decisions D20, D21, D18, D2, D6 (wrong numbers on real offers)',
         process='P2', priority='high', col='todo'),
    dict(title='Owner and date for real catalogue prices (D1, D3, D4)', process='P5', priority='normal', col='backlog'),
    dict(title='Start the cadence: daily huddle, weekly operations review, decision log', process='M1', priority='high', col='doing'),
    dict(title='Baseline 6 KPIs for four weeks (offer cycle time, win rate, projects updated weekly, schedule variance, availability, invoice day)',
         process='M1', priority='normal', col='backlog'),
    dict(title='Write the O&M field process on top of Mont v2 alerts (work order, response time, preventive plan)',
         process='P8', priority='normal', col='backlog'),
    dict(title='Turn the approved process map into an AGS chapter', process='E1', priority='low', col='backlog'),
    dict(title='List the Solar team: names, roles, installers, O&M technicians', process='E4', priority='high', col='todo'),
]
