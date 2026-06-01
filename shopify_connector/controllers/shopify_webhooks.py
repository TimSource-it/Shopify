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
        shop_name = shop_domain.replace('.myshopify.com', '')
        return request.env['shopify.config'].sudo().search([
            ('shop_name', '=', shop_name),
            ('state', '=', 'connected'),
        ], limit=1)

    def _safe_execute(self, func, *args, **kwargs):
        """Voer een functie uit met savepoint voor race condition bescherming."""
        savepoint = f"sp_{id(func)}"
        try:
            request.env.cr.execute(f"SAVEPOINT {savepoint}")
            result = func(*args, **kwargs)
            request.env.cr.execute(f"RELEASE SAVEPOINT {savepoint}")
            return result
        except Exception as e:
            try:
                request.env.cr.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            except Exception:
                pass
            raise e

    def _cancel_order(self, order):
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
                    existing_refunds = order.invoice_ids.filtered(
                        lambda i: i.move_type == 'out_refund'
                    )
                    if not existing_refunds:
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
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')
            if not self._verify_webhook(data, hmac_header):
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
            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"customers/redact fout: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/shop/redact',
                type='http', auth='public', csrf=False, methods=['POST'])
    def shop_redact(self, **kwargs):
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')
            if not self._verify_webhook(data, hmac_header):
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
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')
            if not self._verify_webhook(data, hmac_header):
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
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')
            if not self._verify_webhook(data, hmac_header):
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
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')
            if not self._verify_webhook(data, hmac_header):
                return request.make_response('Unauthorized', status=401)
            order_data = json.loads(data)
            shop_domain = request.httprequest.headers.get('X-Shopify-Shop-Domain', '')
            config = self._get_config_for_shop(shop_domain)
            if config:
                def do_import():
                    request.env['shopify.order.import'].sudo()._import_order(order_data, config)
                    _logger.info(f"Bestelling {order_data.get('order_number')} real-time geïmporteerd")
                self._safe_execute(do_import)
            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"orders/create webhook fout: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/orders/updated',
                type='http', auth='public', csrf=False, methods=['POST'])
    def orders_updated(self, **kwargs):
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')
            if not self._verify_webhook(data, hmac_header):
                return request.make_response('Unauthorized', status=401)
            order_data = json.loads(data)
            shopify_order_id = str(order_data.get('id', ''))
            financial_status = order_data.get('financial_status', '')
            fulfillment_status = order_data.get('fulfillment_status', '') or 'unfulfilled'
            shop_domain = request.httprequest.headers.get('X-Shopify-Shop-Domain', '')
            config = self._get_config_for_shop(shop_domain)

            def do_update():
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

            self._safe_execute(do_update)
            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"orders/updated webhook fout: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/orders/cancelled',
                type='http', auth='public', csrf=False, methods=['POST'])
    def orders_cancelled(self, **kwargs):
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')
            if not self._verify_webhook(data, hmac_header):
                return request.make_response('Unauthorized', status=401)
            order_data = json.loads(data)
            shopify_order_id = str(order_data.get('id', ''))

            def do_cancel():
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

            self._safe_execute(do_cancel)
            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"orders/cancelled webhook fout: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/returns/create',
                type='http', auth='public', csrf=False, methods=['POST'])
    def returns_create(self, **kwargs):
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')
            if not self._verify_webhook(data, hmac_header):
                return request.make_response('Unauthorized', status=401)
            payload = json.loads(data)
            shop_domain = request.httprequest.headers.get('X-Shopify-Shop-Domain', '')
            config = self._get_config_for_shop(shop_domain)
            if not config:
                return request.make_response('OK', status=200)

            order_id = str(payload.get('order_id', ''))
            shopify_return_id = str(payload.get('id', ''))

            def do_return():
                order = request.env['sale.order'].sudo().search([
                    ('shopify_order_id', '=', order_id)
                ], limit=1)
                if not order:
                    _logger.warning(f"Order niet gevonden voor retour: {order_id}")
                    return
                order.sudo().write({'shopify_return_id': shopify_return_id})
                return_line_items = payload.get('return_line_items', [])
                self._create_return_picking(order, return_line_items, config)
                _logger.info(f"Retour aangevraagd voor order {order.name}: {shopify_return_id}")

            self._safe_execute(do_return)
            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"returns/create webhook fout: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/returns/update',
                type='http', auth='public', csrf=False, methods=['POST'])
    def returns_update(self, **kwargs):
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')
            if not self._verify_webhook(data, hmac_header):
                return request.make_response('Unauthorized', status=401)
            payload = json.loads(data)
            order_id = str(payload.get('order_id', ''))
            status = payload.get('status', '')
            order = request.env['sale.order'].sudo().search([
                ('shopify_order_id', '=', order_id)
            ], limit=1)
            if order:
                _logger.info(f"Retour status bijgewerkt voor {order.name}: {status}")
            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"returns/update webhook fout: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/refunds/create',
                type='http', auth='public', csrf=False, methods=['POST'])
    def refunds_create(self, **kwargs):
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')
            if not self._verify_webhook(data, hmac_header):
                return request.make_response('Unauthorized', status=401)
            payload = json.loads(data)
            shop_domain = request.httprequest.headers.get('X-Shopify-Shop-Domain', '')
            config = self._get_config_for_shop(shop_domain)
            if not config:
                return request.make_response('OK', status=200)

            order_id = str(payload.get('order_id', ''))
            refund_line_items = payload.get('refund_line_items', [])

            def do_refund():
                order = request.env['sale.order'].sudo().search([
                    ('shopify_order_id', '=', order_id)
                ], limit=1)
                if not order:
                    _logger.warning(f"Order niet gevonden voor refund: {order_id}")
                    return

                order.sudo().write({'shopify_financial_status': 'refunded'})

                # Controleer of er al een retourlevering bestaat via flow 2
                existing_return_pickings = order.picking_ids.filtered(
                    lambda p: p.picking_type_code == 'incoming' and p.state == 'done'
                )

                if existing_return_pickings:
                    # Flow 2 — retour al verwerkt via Odoo, alleen credit nota checken
                    _logger.info(f"Retour voor {order.name} al verwerkt via Odoo — geen nieuwe retourlevering aanmaken")
                else:
                    # Flow 1 — selfservice retour via Shopify portaal
                    # Maak retourlevering aan en valideer direct
                    if refund_line_items:
                        return_picking = self._create_and_validate_return_picking(
                            order, refund_line_items, config
                        )
                        if return_picking:
                            _logger.info(f"Retourlevering aangemaakt en gevalideerd voor {order.name}: {return_picking.name}")

                # Credit nota aanmaken als die er nog niet is
                existing_refunds = order.invoice_ids.filtered(
                    lambda i: i.move_type == 'out_refund'
                )
                if not existing_refunds:
                    importer = request.env['shopify.order.import'].sudo()
                    importer._process_refund(order, config)
                    _logger.info(f"Refund verwerkt voor order {order.name}")
                else:
                    _logger.info(f"Refund ontvangen voor {order.name} — credit nota al aanwezig")

            self._safe_execute(do_refund)
            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"refunds/create webhook fout: {e}")
            return request.make_response('OK', status=200)

    def _create_return_picking(self, order, return_line_items, config, auto_validate=False):
        """Maak een retourlevering aan in Odoo op basis van Shopify retourverzoek."""
        try:
            original_picking = order.picking_ids.filtered(
                lambda p: p.state == 'done' and p.picking_type_code == 'outgoing'
            )
            if not original_picking:
                order.message_post(
                    body="⚠️ Retour aangevraagd via Shopify maar geen voltooide levering gevonden. Verwerk handmatig.",
                    message_type='comment',
                    subtype_xmlid='mail.mt_note',
                )
                return None

            original_picking = original_picking[0]

            return_wizard = request.env['stock.return.picking'].sudo().with_context(
                active_id=original_picking.id,
                active_model='stock.picking',
            ).create({
                'picking_id': original_picking.id,
            })

            if return_line_items:
                for return_line in return_wizard.product_return_moves:
                    product = return_line.product_id
                    for shopify_line in return_line_items:
                        variant_id = str(shopify_line.get('line_item', {}).get('variant_id', ''))
                        if str(product.shopify_variant_id or '') == variant_id:
                            return_line.quantity = shopify_line.get('quantity', 0)
                            break

            result = return_wizard.create_returns()
            return_picking_id = result.get('res_id')

            if return_picking_id:
                return_picking = request.env['stock.picking'].sudo().browse(return_picking_id)

                if auto_validate:
                    # Stel hoeveelheden in en valideer direct
                    for move in return_picking.move_ids:
                        move.quantity = move.product_uom_qty
                    return_picking.with_context(skip_immediate=True).button_validate()
                    _logger.info(f"Retourlevering gevalideerd voor {order.name}: {return_picking.name}")
                else:
                    order.message_post(
                        body=f"📦 Retour aangevraagd via Shopify — retourlevering aangemaakt: {return_picking.name}",
                        message_type='comment',
                        subtype_xmlid='mail.mt_note',
                    )
                    _logger.info(f"Retourlevering aangemaakt voor {order.name}: {return_picking.name}")

                return return_picking

        except Exception as e:
            _logger.error(f"Retourlevering aanmaken mislukt voor {order.name}: {e}")
            order.message_post(
                body=f"⚠️ Retour aangevraagd via Shopify maar retourlevering aanmaken mislukt: {e}. Verwerk handmatig.",
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )
            return None

    def _create_and_validate_return_picking(self, order, refund_line_items, config):
        """Maak retourlevering aan en valideer direct voor flow 1 (selfservice via Shopify)."""
        # Converteer refund_line_items naar het formaat dat _create_return_picking verwacht
        return_line_items = []
        for item in refund_line_items:
            line_item = item.get('line_item', {})
            return_line_items.append({
                'quantity': item.get('quantity', 0),
                'line_item': {
                    'variant_id': str(line_item.get('variant_id', '')),
                }
            })

        return self._create_return_picking(
            order, return_line_items, config, auto_validate=True
        )
