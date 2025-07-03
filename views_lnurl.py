from http import HTTPStatus
from math import ceil

from fastapi import APIRouter, HTTPException, Query, Request
from lnbits.core.services import create_invoice
from lnbits.helpers import urlsafe_short_hash
from lnbits.lnurl import LnurlErrorResponseHandler
from lnbits.utils.crypto import AESCipher
from lnbits.utils.exchange_rates import fiat_amount_as_satoshis
from loguru import logger

from .crud import (
    create_lnpos_payment,
    get_lnpos,
    get_lnpos_payment,
    update_lnpos_payment,
)
from .models import LnposPayment

lnpos_lnurl_router = APIRouter(prefix="/api/v1/lnurl")
lnpos_lnurl_router.route_class = LnurlErrorResponseHandler


@lnpos_lnurl_router.get("/{lnpos_id}")
async def lnurl_params(
    request: Request,
    lnpos_id: str,
    payload: str = Query(..., alias="p"),
):
    logger.info(f"LNURL request received for lnpos_id: {lnpos_id}")
    logger.info(f"Payload received: {payload}")
    
    lnpos = await get_lnpos(lnpos_id)
    if not lnpos:
        logger.error(f"LnPos not found for id: {lnpos_id}")
        raise HTTPException(HTTPStatus.NOT_FOUND, "lnpos not found.")

    try:
        aes = AESCipher(lnpos.key)
        msg = aes.decrypt(payload, urlsafe=True)
        logger.info(f"Decrypted message: {msg}")
    except Exception as e:
        logger.error(f"Error decrypting payload: {e}")
        logger.error(f"Payload: {payload}")
        logger.error(f"LnPos key: {lnpos.key}")
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Invalid payload.") from e

    try:
        pin, amount_in_cent = msg.split(":")
        logger.info(f"Parsed pin: {pin}, amount_in_cent: {amount_in_cent}")
    except ValueError as e:
        logger.error(f"Error parsing decrypted message: {msg}")
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Invalid payload format.") from e

    price_sat = (
        await fiat_amount_as_satoshis(float(amount_in_cent) / 100, lnpos.currency)
        if lnpos.currency != "sat"
        else ceil(float(amount_in_cent))
    )
    if price_sat is None:
        logger.error(f"Price fetch error for amount: {amount_in_cent}, currency: {lnpos.currency}")
        raise HTTPException(HTTPStatus.BAD_REQUEST, detail="Price fetch error.")

    price_sat = int(price_sat * ((lnpos.profit / 100) + 1))
    price_msat = price_sat * 1000
    
    logger.info(f"Calculated price_sat: {price_sat}, price_msat: {price_msat}")

    lnpos_payment = LnposPayment(
        id=urlsafe_short_hash(),
        lnpos_id=lnpos.id,
        sats=price_sat,
        pin=int(pin),
    )
    # Store original request data for invoice creation
    lnpos_payment.original_amount_cents = float(amount_in_cent)
    
    await create_lnpos_payment(lnpos_payment)
    
    callback_url = str(request.url_for("lnpos.lnurl_callback", payment_id=lnpos_payment.id))
    
    # Ensure metadata is properly formatted JSON string according to LUD-06
    # Use proper JSON construction to avoid escaping issues
    import json
    metadata_array = [["text/plain", lnpos.title]]
    metadata_json = json.dumps(metadata_array)
    
    # Validate metadata JSON
    try:
        json.loads(metadata_json)
        logger.info(f"Metadata JSON is valid: {metadata_json}")
    except json.JSONDecodeError as e:
        logger.error(f"Invalid metadata JSON: {metadata_json}, error: {e}")
        raise HTTPException(HTTPStatus.INTERNAL_SERVER_ERROR, "Invalid metadata format.")
    
    response_data = {
        "tag": "payRequest",
        "callback": callback_url,
        "minSendable": price_msat,
        "maxSendable": price_msat,
        "metadata": metadata_json,
    }
    
    logger.info(f"LNURL-pay response for {lnpos_id}:")
    logger.info(f"  tag: {response_data['tag']}")
    logger.info(f"  callback: {response_data['callback']}")
    logger.info(f"  minSendable: {response_data['minSendable']}")
    logger.info(f"  maxSendable: {response_data['maxSendable']}")
    logger.info(f"  metadata: {response_data['metadata']}")
    
    # Validate the complete response
    required_fields = ["tag", "callback", "minSendable", "maxSendable", "metadata"]
    for field in required_fields:
        if field not in response_data:
            logger.error(f"Missing required field: {field}")
            raise HTTPException(HTTPStatus.INTERNAL_SERVER_ERROR, f"Missing required field: {field}")
    
    logger.info(f"Complete LNURL-pay response: {response_data}")
    
    return response_data


@lnpos_lnurl_router.get(
    "/cb/{payment_id}",
    status_code=HTTPStatus.OK,
    name="lnpos.lnurl_callback",
)
async def lnurl_callback(
    request: Request, 
    payment_id: str,
    amount: int = Query(..., description="Amount in millisatoshis")
):
    lnpos_payment = await get_lnpos_payment(payment_id)
    if not lnpos_payment:
        raise HTTPException(HTTPStatus.NOT_FOUND, detail="lnpos_payment not found.")
    lnpos = await get_lnpos(lnpos_payment.lnpos_id)
    if not lnpos:
        raise HTTPException(HTTPStatus.NOT_FOUND, detail="lnpos not found.")

    # Validate amount matches expected amount
    expected_amount_msat = lnpos_payment.sats * 1000
    if amount != expected_amount_msat:
        raise HTTPException(
            HTTPStatus.BAD_REQUEST, 
            detail=f"Amount mismatch. Expected {expected_amount_msat} msat, got {amount} msat"
        )

    pin_display_url = str(request.url_for("lnpos.displaypin", payment_id=payment_id))
    
    # Use preserved original request data for invoice extra_json
    extra_data = {
        "tag": "PoS",
        "pos": {
            "callback_url": pin_display_url,
            "pin": lnpos_payment.pin,
            "pos_id": lnpos_payment.lnpos_id,
            "requested_amount": lnpos_payment.original_amount_cents,
            "requested_currency": lnpos.currency
        }
    }

    payment = await create_invoice(
        wallet_id=lnpos.wallet,
        amount=lnpos_payment.sats,
        memo=lnpos.title,
        unhashed_description=lnpos.lnurlpay_metadata.encode(),
        extra=extra_data,
    )
    lnpos_payment.payment_hash = payment.payment_hash
    lnpos_payment = await update_lnpos_payment(lnpos_payment)
    return {
        "pr": payment.bolt11,
        "successAction": {
            "tag": "url",
            "description": "Check the attached link for the pin.",
            "url": pin_display_url,
        },
        "routes": [],
    }
