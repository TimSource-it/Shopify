from odoo import http
from odoo.http import request
import json
import hmac
import hashlib
import logging
import base64

_logger = logging.getLogger(__name__)


class ShopifyWebhookController(http.Controller):

    def _verify_webhook(self, data, hmac_header):
        """Verifieert de Shopify webhook HMAC signature."""
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
            _logger.error(f"Webhook verificatie fout: {e}")
            return False

    @http.route('/shopify/webhooks/customers/redact',
                type='http', auth='public', csrf=False, methods=['POST'])
    def customers_redact(self, **kwargs):
        """GDPR: Verwijder klantgegevens op verzoek van Shopify."""
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')

            if not self._verify_webhook(data, hmac_header):
                _logger.warning("Webhook HMAC verificatie mislukt voor customers/redact")
                return request.make_response('Unauthorized', status=401)

            payload = json.loads(data)
            shopify_customer_id = str(payload.get('customer', {}).get('id', ''))

            if shopify_customer_id:
                partner = request.env['res.partner'].sudo().search([
                    ('shopify_customer_id', '=', shopify_customer_id)
                ], limit=1)
                if partner:
                    partner.sudo().write({
                        'name': 'Verwijderde Klant',
                        'email': False,
                        'phone': False,
                        'street': False,
                        'street2': False,
                        'city': False,
                        'zip': False,
                        'shopify_customer_id': False,
                    })
                    _logger.info(f"Klant {shopify_customer_id} geanonimiseerd via GDPR verzoek")

            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"customers/redact fout: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/shop/redact',
                type='http', auth='public', csrf=False, methods=['POST'])
    def shop_redact(self, **kwargs):
        """GDPR: Verwijder winkelgegevens na deïnstallatie."""
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')

            if not self._verify_webhook(data, hmac_header):
                _logger.warning("Webhook HMAC verificatie mislukt voor shop/redact")
                return request.make_response('Unauthorized', status=401)

            payload = json.loads(data)
            shop_domain = payload.get('shop_domain', '')
            shop_name = shop_domain.replace('.myshopify.com', '')

            _logger.info(f"Shop redact verzoek ontvangen voor: {shop_name}")

            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"shop/redact fout: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/customers/data_request',
                type='http', auth='public', csrf=False, methods=['POST'])
    def customers_data_request(self, **kwargs):
        """GDPR: Klant vraagt zijn gegevens op."""
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')

            if not self._verify_webhook(data, hmac_header):
                _logger.warning("Webhook HMAC verificatie mislukt voor customers/data_request")
                return request.make_response('Unauthorized', status=401)

            payload = json.loads(data)
            shopify_customer_id = str(payload.get('customer', {}).get('id', ''))
            _logger.info(f"Data request ontvangen voor klant: {shopify_customer_id}")

            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"customers/data_request fout: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/app/uninstalled',
                type='http', auth='public', csrf=False, methods=['POST'])
    def app_uninstalled(self, **kwargs):
        """Cleanup bij verwijdering van de app."""
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')

            if not self._verify_webhook(data, hmac_header):
                _logger.warning("Webhook HMAC verificatie mislukt voor app/uninstalled")
                return request.make_response('Unauthorized', status=401)

            payload = json.loads(data)
            shop_domain = payload.get('domain', '')
            shop_name = shop_domain.replace('.myshopify.com', '')

            config = request.env['shopify.config'].sudo().search([
                ('shop_name', '=', shop_name)
            ], limit=1)

            if config:
                config.sudo().write({
                    'access_token': False,
                    'refresh_token': False,
                    'access_token_expires_at': False,
                    'state': 'draft',
                })
                _logger.info(f"App verwijderd voor winkel: {shop_name}")

            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"app/uninstalled fout: {e}")
            return request.make_response('OK', status=200)
