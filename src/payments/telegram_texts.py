"""Telegram checkout copy. Values are Telegram HTML; callers escape every argument."""
# ruff: noqa: E501

LANGUAGES = ("en", "ru")

TEXTS = {
    "en": {
        "choose_amount": "Choose the top-up amount:",
        "custom": "Other amount",
        "custom_prompt": "Send the amount in {currency}, from {min} to {max}. Example: {example}",
        "custom_invalid": "That is not an amount. Send a number with up to two decimals, e.g. {example}",
        "custom_range": "The amount must be from {min} to {max}.",
        "amount": "Amount",
        "choose_method": "Choose how to pay:",
        "fee": "fee",
        "to_pay": "To pay",
        "invoice": "Invoice",
        "pay_via": "Pay via {method}",
        "custodial_hint": "Pay with the button below, then press «Check payment».",
        "send": "Send",
        "network": "Network",
        "address": "Address:",
        "pay_before": "Pay before",
        "onchain_note": (
            "Send exactly this amount of {token} on {network}: it must arrive in full. "
            "The network fee is on you — if an exchange deducts a withdrawal fee, add it on top. "
            "Less is not credited; anything above the amount is credited too. "
            "Leave the comment / memo empty."
        ),
        "onchain_auto": "The balance is credited automatically after network confirmation.",
        "underpaid": "Received {received} of {amount} {token}. Send the remaining {missing} {token}.",
        "wrong_token": "A {got} transfer arrived instead of {token}; it is not credited. Contact support.",
        "check": "Check payment",
        "back": "Back",
        "cancel": "Cancel",
        "new": "New payment",
        "not_received": "Payment not received yet. Try again in a minute.",
        "not_received_toast": "Not received yet",
        "paid": "Payment received. {amount} will be credited to your balance.",
        "expired": "This invoice has expired and was not paid. Create a new one if you still want to top up.",
        "review": "The payment needs a manual review. Please contact support and quote the invoice number.",
        "failed": "Could not create the invoice. Try again or choose another method.",
        "cancelled": "Payment cancelled.",
        "session_expired": "This payment menu has expired.",
        "foreign": "This menu belongs to another user.",
        "unavailable": "This option is no longer available.",
        "wait": "Please wait…",
        "unknown": "Unknown action.",
    },
    "ru": {
        "choose_amount": "Выберите сумму пополнения:",
        "custom": "Другая сумма",
        "custom_prompt": "Отправьте сумму в {currency}, от {min} до {max}. Например: {example}",
        "custom_invalid": "Это не сумма. Отправьте число с не более чем двумя знаками после точки, например {example}",
        "custom_range": "Сумма должна быть от {min} до {max}.",
        "amount": "Сумма",
        "choose_method": "Выберите способ оплаты:",
        "fee": "комиссия",
        "to_pay": "К оплате",
        "invoice": "Счёт",
        "pay_via": "Оплатить через {method}",
        "custodial_hint": "Оплатите кнопкой ниже, затем нажмите «Проверить оплату».",
        "send": "Отправьте",
        "network": "Сеть",
        "address": "Адрес:",
        "pay_before": "Оплатить до",
        "onchain_note": (
            "Отправьте ровно эту сумму {token} в сети {network}: она должна прийти полностью. "
            "Комиссия сети — за ваш счёт: если биржа удерживает комиссию за вывод, добавьте её сверху. "
            "Меньшая сумма не зачисляется, излишек тоже зачисляется. "
            "Комментарий / memo оставьте пустым."
        ),
        "onchain_auto": "Баланс пополнится автоматически после подтверждения в сети.",
        "underpaid": "Получено {received} из {amount} {token}. Доплатите {missing} {token}.",
        "wrong_token": "Пришёл перевод {got} вместо {token}; он не зачисляется. Напишите в поддержку.",
        "check": "Проверить оплату",
        "back": "Назад",
        "cancel": "Отмена",
        "new": "Новый платёж",
        "not_received": "Оплата ещё не получена. Попробуйте через минуту.",
        "not_received_toast": "Оплата ещё не получена",
        "paid": "Оплата получена. {amount} будет зачислено на баланс.",
        "expired": "Срок счёта истёк, оплата не поступила. Создайте новый счёт, если хотите пополнить баланс.",
        "review": "Платёж требует ручной проверки. Напишите в поддержку и укажите номер счёта.",
        "failed": "Не удалось создать счёт. Попробуйте снова или выберите другой способ.",
        "cancelled": "Платёж отменён.",
        "session_expired": "Это меню оплаты устарело.",
        "foreign": "Это меню другого пользователя.",
        "unavailable": "Этот вариант больше недоступен.",
        "wait": "Подождите…",
        "unknown": "Неизвестное действие.",
    },
}

CHAIN_NAMES = {"ton": "TON", "solana": "Solana"}


def text(lang: str, key: str, **values: str) -> str:
    template = TEXTS.get(lang, TEXTS["en"])[key]
    return template.format(**values) if values else template
