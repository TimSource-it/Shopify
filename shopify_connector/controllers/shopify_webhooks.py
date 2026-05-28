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

    def _get_config_for_shop(self, shop_domain):
        """Haal de config op voor een shop domain."""
        shop_name = shop_domain.replace('.myshopify.com', '')
        return request.env['shopify.config'].sudo().search([
            ('shop_name', '=', shop_name),
            ('state', '=', 'connected'),
        ], limit=1)

    def _cancel_order(self, order):
        """Annuleer een order op basis van de huidige status."""
        try:
            if order.state == 'draft':
                order.action_cancel()
                _logger.info(f"Order {order.name} geannuleerd (was draft)")
                return

            if order.state == 'sale':
                invoices = order.invoice_ids.filtered(
                    lambda i: i.state == 'posted' and i.move_type == 'out_invoice'
                )
                if invoices:
                    for invoice in invoices:
                        refund = invoice._reverse_moves()
                        refund.action_post()
                        _logger.info(f"Credit nota aangemaakt voor {invoice.name}")

                for picking in order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')):
                    picking.action_cancel()

                order.action_cancel()
                _logger.info(f"Order {order.name} geannuleerd (was sale)")
                return

            if order.state == 'done':
                order.write({
                    'shopify_financial_status': 'cancelled',
                    'shopify_fulfillment_status': 'cancelled',
                })
                order.message_post(
                    body="⚠️ Deze order is geannuleerd in Shopify maar is al verzonden. Verwerk het retour handmatig.",
                    message_type='comment',
                    subtype_xmlid='mail.mt_note',
                )
                _logger.info(f"Order {order.name} al verzonden — handmatige actie vereist")

        except Exception as e:
            _logger.error(f"Order annuleren mislukt voor {order.name}: {e}")
            order.message_post(
                body=f"⚠️ Automatisch annuleren mislukt: {e}. Verwerk handmatig.",
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

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
            _logger.info(f"Shop redact verzoek ontvangen voor: {shop_domain}")

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
                config.sudo()._unregister_carrier_service()
                config.sudo().write({
                    'access_token': False,
                    'refresh_token': False,
                    'access_token_expires_at': False,
                    'state': 'draft',
                    'shopify_carrier_service_id': False,
                })
                _logger.info(f"App verwijderd voor winkel: {shop_name}")

            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"app/uninstalled fout: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/orders/create',
                type='http', auth='public', csrf=False, methods=['POST'])
    def orders_create(self, **kwargs):
        """Real-time import van nieuwe Shopify bestelling."""
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')

            if not self._verify_webhook(data, hmac_header):
                _logger.warning("Webhook HMAC verificatie mislukt voor orders/create")
                return request.make_response('Unauthorized', status=401)

            order_data = json.loads(data)
            shop_domain = request.httprequest.headers.get('X-Shopify-Shop-Domain', '')
            config = self._get_config_for_shop(shop_domain)

            if config:
                request.env['shopify.order.import'].sudo()._import_order(order_data, config)
                _logger.info(f"Bestelling {order_data.get('order_number')} real-time geïmporteerd")

            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"orders/create webhook fout: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/orders/updated',
                type='http', auth='public', csrf=False, methods=['POST'])
    def orders_updated(self, **kwargs):
        """Update van bestaande Shopify bestelling."""
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')

            if not self._verify_webhook(data, hmac_header):
                _logger.warning("Webhook HMAC verificatie mislukt voor orders/updated")
                return request.make_response('Unauthorized', status=401)

            order_data = json.loads(data)
            shopify_order_id = str(order_data.get('id', ''))
            financial_status = order_data.get('financial_status', '')
            fulfillment_status = order_data.get('fulfillment_status', '') or 'unfulfilled'
            shop_domain = request.httprequest.headers.get('X-Shopify-Shop-Domain', '')
            config = self._get_config_for_shop(shop_domain)

            order = request.env['sale.order'].sudo().search([
                ('shopify_order_id', '=', shopify_order_id)
            ], limit=1)

            if order:
                order.sudo().write({
                    'shopify_financial_status': financial_status,
                    'shopify_fulfillment_status': fulfillment_status,
                })
                if config and order.state == 'draft':
                    importer = request.env['shopify.order.import'].sudo()
                    if importer._should_confirm_order(config, financial_status):
                        order.sudo().action_confirm()
                        _logger.info(f"Order {order.name} alsnog bevestigd via orders/updated webhook")
                        if config.invoice_policy == 'on_confirm':
                            importer._create_invoice(order, config)
                _logger.info(f"Bestelling {order.name} bijgewerkt: {financial_status} / {fulfillment_status}")
            else:
                if config:
                    request.env['shopify.order.import'].sudo()._import_order(order_data, config)

            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"orders/updated webhook fout: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/orders/cancelled',
                type='http', auth='public', csrf=False, methods=['POST'])
    def orders_cancelled(self, **kwargs):
        """Shopify bestelling geannuleerd."""
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')

            if not self._verify_webhook(data, hmac_header):
                _logger.warning("Webhook HMAC verificatie mislukt voor orders/cancelled")
                return request.make_response('Unauthorized', status=401)

            order_data = json.loads(data)
            shopify_order_id = str(order_data.get('id', ''))

            order = request.env['sale.order'].sudo().search([
                ('shopify_order_id', '=', shopify_order_id)
            ], limit=1)

            if order:
                order.sudo().write({
                    'shopify_financial_status': 'cancelled',
                    'shopify_fulfillment_status': 'cancelled',
                })
                self._cancel_order(order.sudo())
                _logger.info(f"Annulering verwerkt voor {order.name}")

            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"orders/cancelled webhook fout: {e}")
            return request.make_response('OK', status=200)
