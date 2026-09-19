"""The AI call to the artisan (simulated): a script, a log line, no phone.

The three calls the README asks for - order confirmed, pickup scheduled,
payment credited - are built as data from the order, the shipment and the
payout, then written to `logs/calls.jsonl` as if they had been placed.
Nothing dials and nothing synthesises audio; a real integration would take
`script` and hand it to a TTS + telephony provider.

Rules carried over from the rest of the project:

* **A missing amount is left out, never spoken as zero.** If C1 did not
  carry a take-home figure the call says the order is confirmed and says
  nothing about money, rather than telling an artisan she will be paid
  Rs 0 or a guess.
* **Every script is in the artisan's language where C2 has one.** Scripts
  exist in Hindi and English. An artisan who speaks Hindi is called in
  Hindi, else English if she speaks it, else Hindi - and the log records
  the language actually used, so a Bengali-only artisan being called in
  Hindi shows up as a gap rather than being hidden.
* **Artisan phone numbers are simulated.** C1's seed has none (B2 owns
  that record), so a stable fake number is derived from the artisan id.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

from c2 import c1_client
from c2.fmt import inr
from c2.logs import append_jsonl
from c2.models import CallLog, Order, Payout, Shipment

EVENTS = ("order_confirmed", "pickup_scheduled", "payment_credited")

_HI_MONTHS = (
    "जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून",
    "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर",
)
_EN_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

_CALLS: list[CallLog] = []


def _hi_date(d: date) -> str:
    return f"{d.day} {_HI_MONTHS[d.month - 1]}"


def _en_date(d: date) -> str:
    return f"{d.day} {_EN_MONTHS[d.month - 1]}"


def _phone(artisan_id: str) -> str:
    n = int(hashlib.sha1(artisan_id.encode()).hexdigest(), 16) % 10**9
    digits = "9" + str(n).zfill(9)  # ten digits, starting 9 like an Indian mobile
    return f"+91 {digits[:5]} {digits[5:]}"


def pick_language(languages: list[str]) -> str:
    if "hi" in languages:
        return "hi"
    if "en" in languages:
        return "en"
    return "hi"


def _items(payout: Payout, lang: str) -> str:
    titles = payout.titles_hi if lang == "hi" and payout.titles_hi else payout.titles_en
    return ", ".join(titles) if titles else ""


def _money_hi(payout: Payout, tail: str) -> str:
    return "" if payout.amount_inr is None else f" {tail.format(amt=inr(payout.amount_inr))}"


def script(event: str, order: Order, payout: Payout, shipment: Shipment | None, lang: str) -> str:
    name = payout.artisan_name
    city = order.buyer.city or ""
    items = _items(payout, lang)
    stop = next((s for s in (shipment.pickups if shipment else []) if s.artisan_id == payout.artisan_id), None)

    if lang == "hi":
        head = f"नमस्ते {name} जी, यह क्राफ्टली की ओर से कॉल है।"
        if event == "order_confirmed":
            where = f" {city} से" if city else ""
            return (
                f"{head} आपके लिए{where} एक नया ऑर्डर आया है: {payout.quantity} नग, {items}। "
                f"ऑर्डर नंबर {order.order_id}।"
                + _money_hi(payout, "इसके लिए आपको {amt} मिलेंगे।")
                + " कृपया तैयारी शुरू करें। पुष्टि के लिए 1 दबाएँ।"
            )
        if event == "pickup_scheduled" and shipment and stop:
            return (
                f"{head} ऑर्डर {order.order_id} का कूरियर पिकअप {_hi_date(shipment.pickup_date)} को "
                f"{shipment.pickup_window} के बीच तय हुआ है। पैकेट पर यह नंबर लिखें: {stop.waybill}। "
                f"कूरियर: {shipment.courier}।"
            )
        if event == "payment_credited":
            return (
                f"{head} बधाई हो! ऑर्डर {order.order_id} का सामान पहुँच गया है"
                + (f" और {inr(payout.amount_inr)} आपके खाते में जमा कर दिए गए हैं।" if payout.amount_inr is not None
                   else "। भुगतान की राशि की पुष्टि हमारी टीम अलग से करेगी।")
                + " धन्यवाद।"
            )
    else:
        head = f"Hello {name}, this is a call from Craftly."
        if event == "order_confirmed":
            where = f" from {city}" if city else ""
            money = "" if payout.amount_inr is None else f" You will receive {inr(payout.amount_inr)} for it."
            return (
                f"{head} You have a new order{where}: {payout.quantity} x {items}. "
                f"Order number {order.order_id}.{money} Please start preparing. Press 1 to confirm."
            )
        if event == "pickup_scheduled" and shipment and stop:
            return (
                f"{head} The courier pickup for order {order.order_id} is set for "
                f"{_en_date(shipment.pickup_date)}, between {shipment.pickup_window}. "
                f"Write this number on the parcel: {stop.waybill}. Courier: {shipment.courier}."
            )
        if event == "payment_credited":
            money = (
                f" and {inr(payout.amount_inr)} has been credited to your account."
                if payout.amount_inr is not None
                else ". Our team will confirm the payment amount separately."
            )
            return f"{head} Good news! Order {order.order_id} has been delivered{money} Thank you."
    raise ValueError(f"Cannot script '{event}' (unknown event, or pickup call without a shipment)")


def place_call(
    event: str, order: Order, payout: Payout, shipment: Shipment | None = None, at: datetime | None = None
) -> CallLog:
    if event not in EVENTS:
        raise ValueError(f"unknown call event '{event}'; use one of {EVENTS}")
    lang = pick_language(payout.languages)
    text = script(event, order, payout, shipment, lang)
    text_en = text if lang == "en" else script(event, order, payout, shipment, "en")
    call_id = "call_" + hashlib.sha1(f"{event}|{order.order_id}|{payout.artisan_id}".encode()).hexdigest()[:10]
    already = next((c for c in _CALLS if c.call_id == call_id), None)
    if already is not None:  # idempotent, like the courier booking
        return already
    log = CallLog(
        call_id=call_id,
        event=event,
        order_id=order.order_id,
        artisan_id=payout.artisan_id,
        artisan_name=payout.artisan_name,
        to_phone=_phone(payout.artisan_id),
        language=lang,
        script=text,
        script_en=text_en,
        duration_sec=max(8, len(text_en) // 14),
        outcome="ACKNOWLEDGED (pressed 1)" if event == "order_confirmed" else "MESSAGE_DELIVERED",
        at=at or datetime.now(timezone.utc),
    )
    _CALLS.append(log)
    append_jsonl("calls.jsonl", log.model_dump(mode="json"))
    return log


def calls_for(order_id: str) -> list[CallLog]:
    return [c for c in _CALLS if c.order_id == order_id]


def reset() -> None:
    _CALLS.clear()


def note_language_gaps(payouts: list[Payout]) -> list[str]:
    """Artisans the call will not be in their own language."""
    return [
        f"{p.artisan_name} speaks {', '.join(p.languages)}; called in {pick_language(p.languages)}"
        for p in payouts
        if p.languages and pick_language(p.languages) not in p.languages
    ]
