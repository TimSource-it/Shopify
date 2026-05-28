from odoo import http
from odoo.http import request
import json
import hmac
import hashlib
import base64
import logging

_logger = logging.getLogger(__name__)


class ShopifyCarrierController(http.Controller):

    def _verify_carrier_request(self, data, hmac_header):
        """Verifieert de HMAC van het carrier verzoek."""
        try:
            client_secret = request.env['ir.config_parameter'].sudo().get_param('shopify.client_secret')
            if not client_secret:
                return False
            digest = hmac.new(
                client_secret.encode('utf-8'),
                data,
                hashlib.sha256
            ).digest()
            computed = base64.b64encode(digest).decode('utf-8')
            return hmac.compare_digest(computed, hmac_header)
        except Exception as e:
            _logger.error(f"Carrier HMAC verificatie fout: {e}")
            return False

    def _get_config_for_shop(self, shop_domain):
        """Haal de config op voor een shop domain."""
        shop_name = shop_domain.replace('.myshopify.com', '')
        return request.env['shopify.config'].sudo().search([
            ('shop_name', '=', shop_name),
            ('state', '=', 'connected'),
        ], limit=1)

    @http.route('/shopify/carrier/rates',
                type='http', auth='public', csrf=False, methods=['POST'])
    def carrier_rates(self, **kwargs):
        """
        Shopify CarrierService callback.
        Shopify stuurt adres + producten, wij sturen beschikbare carriers terug.
        """
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')

            if not self._verify_carrier_request(data, hmac_header):
                _logger.warning("CarrierService HMAC verificatie mislukt")
                return request.make_response(
                    json.dumps({'rates': []}),
                    headers={'Content-Type': 'application/json'},
                    status=401
                )

            payload = json.loads(data)
            rate_request = payload.get('rate', {})

            destination = rate_request.get('destination', {})
            items = rate_request.get('items', [])
            currency = rate_request.get('currency', 'EUR')

            shop_domain = request.httprequest.headers.get('X-Shopify-Shop-Domain', '')
            config = self._get_config_for_shop(shop_domain)

            if not config:
                _logger.warning(f"Geen config gevonden voor shop: {shop_domain}")
                return request.make_response(
                    json.dumps({'rates': []}),
                    headers={'Content-Type': 'application/json'},
                    status=200
                )

            rates = request.env['shopify.carrier.service'].sudo()._get_available_rates(
                destination, items, currency, config
            )

            _logger.info(f"CarrierService: {len(rates)} opties voor {destination.get('city')}")

            return request.make_response(
                json.dumps({'rates': rates}),
                headers={'Content-Type': 'application/json'},
                status=200
            )

        except Exception as e:
            _logger.error(f"CarrierService fout: {e}")
            return request.make_response(
                json.dumps({'rates': []}),
                headers={'Content-Type': 'application/json'},
                status=200
            )
