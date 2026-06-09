from odoo import http
from odoo.http import request
import json
import hmac
import hashlib
import logging
import base64

_logger = logging.getLogger(__name__)


class ShopifyWebhookController(http.Controller):

    def _verify_webhook(self, data, hmac_header, config=None):
        """Verify Shopify webhook HMAC signature."""
        try:
            # Use client_secret from config if available, otherwise fall back to system parameter
            if config and config.client_secret:
                client_secret = config.client_secret
            else:
                client_secret = request.env['ir.config_parameter'].sudo().get_param('shopify.client_secret')

            if not client_secret:
                _logger.warning("No client_secret found for webhook verification — allowing request")
                return True

            digest = hmac.new(
                client_secret.encode('utf-8'),
                data,
                hashlib.sha256
            ).digest()
            computed = base64.b64encode(digest).decode('utf-8')
            return hmac.compare_digest(computed, hmac_header)
        except Exception as e:
            _logger.error(f"Webhook verification error: {e}")
            return False

    def _get_config_for_shop(self, shop_domain):
        """Find the shopify.config record for a given shop domain."""
        if not shop_domain:
            return request.env['shopify.config'].sudo().search([
                ('state', '=', 'connected'),
            ], limit=1)
        shop_name = shop_domain.replace('.myshopify.com', '')
        return request.env['shopify.config'].sudo().search([
            ('shop_name', '=', shop_name),
            ('state', '=', 'connected'),
        ], limit=1)

    def _safe_execute(self, func, *args, **kwargs):
        """Execute a function with savepoint for race condition protection."""
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
        """Cancel an order and handle invoices if needed."""
        try:
            if order.state == 'draft':
                order.action_cancel()
                _logger.info(f"Order {order.name} cancelled (was draft)")
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
                            _logger.info(f"Credit note created for {invoice.name}")

                for picking in order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')):
                    picking.action_cancel()

                order.action_cancel()
                _logger.info(f"Order {order.name} cancelled (was sale)")
                return

            if order.state == 'done':
                order.write({
                    'shopify_financial_status': 'cancelled',
                    'shopify_fulfillment_status': 'cancelled',
                })
                order.message_post(
                    body="⚠️ This order was cancelled in Shopify but has already been shipped. Please process the return manually.",
                    message_type='comment',
                    subtype_xmlid='mail.mt_note',
                )
                _logger.info(f"Order {order.name} already shipped — manual action required")

        except Exception as e:
            _logger.error(f"Order cancellation failed for {order.name}: {e}")
            order.message_post(
                body=f"⚠️ Automatic cancellation failed: {e}. Please process manually.",
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    @http.route('/shopify/webhooks/customers/redact',
                type='http', auth='public', csrf=False, methods=['POST'])
    def customers_redact(self, **kwargs):
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')
            shop_domain = request.httprequest.headers.get('X-Shopify-Shop-Domain', '')
            config = self._get_config_for_shop(shop_domain)
            if not self._verify_webhook(data, hmac_header, config):
                return request.make_response('Unauthorized', status=401)
            payload = json.loads(data)
            shopify_customer_id = str(payload.get('customer', {}).get('id', ''))
            if shopify_customer_id:
                partner = request.env['res.partner'].sudo().search([
                    ('shopify_customer_id', '=', shopify_customer_id)
                ], limit=1)
                if partner:
                    partner.sudo().write({
                        'name': 'Deleted Customer',
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
            _logger.error(f"customers/redact error: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/shop/redact',
                type='http', auth='public', csrf=False, methods=['POST'])
    def shop_redact(self, **kwargs):
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')
            shop_domain = request.httprequest.headers.get('X-Shopify-Shop-Domain', '')
            config = self._get_config_for_shop(shop_domain)
            if not self._verify_webhook(data, hmac_header, config):
                return request.make_response('Unauthorized', status=401)
            payload = json.loads(data)
            shop_domain = payload.get('shop_domain', '')
            _logger.info(f"Shop redact request received for: {shop_domain}")
            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"shop/redact error: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/customers/data_request',
                type='http', auth='public', csrf=False, methods=['POST'])
    def customers_data_request(self, **kwargs):
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')
            shop_domain = request.httprequest.headers.get('X-Shopify-Shop-Domain', '')
            config = self._get_config_for_shop(shop_domain)
            if not self._verify_webhook(data, hmac_header, config):
                return request.make_response('Unauthorized', status=401)
            payload = json.loads(data)
            shopify_customer_id = str(payload.get('customer', {}).get('id', ''))
            _logger.info(f"Data request received for customer: {shopify_customer_id}")
            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"customers/data_request error: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/app/uninstalled',
                type='http', auth='public', csrf=False, methods=['POST'])
    def app_uninstalled(self, **kwargs):
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')
            shop_domain = request.httprequest.headers.get('X-Shopify-Shop-Domain', '')
            config = self._get_config_for_shop(shop_domain)
            if not self._verify_webhook(data, hmac_header, config):
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
                _logger.info(f"App uninstalled for shop: {shop_name}")
            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"app/uninstalled error: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/orders/create',
                type='http', auth='public', csrf=False, methods=['POST'])
    def orders_create(self, **kwargs):
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')
            shop_domain = request.httprequest.headers.get('X-Shopify-Shop-Domain', '')
            config = self._get_config_for_shop(shop_domain)
            if not self._verify_webhook(data, hmac_header, config):
                return request.make_response('Unauthorized', status=401)
            order_data = json.loads(data)
            if config:
                def do_import():
                    request.env['shopify.order.import'].sudo()._import_order(order_data, config)
                    _logger.info(f"Order {order_data.get('order_number')} imported in real-time")
                self._safe_execute(do_import)
            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"orders/create webhook error: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/orders/updated',
                type='http', auth='public', csrf=False, methods=['POST'])
    def orders_updated(self, **kwargs):
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')
            shop_domain = request.httprequest.headers.get('X-Shopify-Shop-Domain', '')
            config = self._get_config_for_shop(shop_domain)
            if not self._verify_webhook(data, hmac_header, config):
                return request.make_response('Unauthorized', status=401)
            order_data = json.loads(data)
            shopify_order_id = str(order_data.get('id', ''))
            financial_status = order_data.get('financial_status', '')
            fulfillment_status = order_data.get('fulfillment_status', '') or 'unfulfilled'

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
                            _logger.info(f"Order {order.name} confirmed via orders/updated webhook")
                            if config.invoice_policy == 'on_confirm':
                                importer._create_invoice(order, config)
                    _logger.info(f"Order {order.name} updated: {financial_status} / {fulfillment_status}")
                else:
                    if config:
                        request.env['shopify.order.import'].sudo()._import_order(order_data, config)

            self._safe_execute(do_update)
            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"orders/updated webhook error: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/orders/cancelled',
                type='http', auth='public', csrf=False, methods=['POST'])
    def orders_cancelled(self, **kwargs):
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')
            shop_domain = request.httprequest.headers.get('X-Shopify-Shop-Domain', '')
            config = self._get_config_for_shop(shop_domain)
            if not self._verify_webhook(data, hmac_header, config):
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
                    _logger.info(f"Cancellation processed for {order.name}")

            self._safe_execute(do_cancel)
            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"orders/cancelled webhook error: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/returns/create',
                type='http', auth='public', csrf=False, methods=['POST'])
    def returns_create(self, **kwargs):
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')
            shop_domain = request.httprequest.headers.get('X-Shopify-Shop-Domain', '')
            config = self._get_config_for_shop(shop_domain)
            if not self._verify_webhook(data, hmac_header, config):
                return request.make_response('Unauthorized', status=401)
            if not config:
                return request.make_response('OK', status=200)
            payload = json.loads(data)
            order_id = str(payload.get('order_id', ''))
            shopify_return_id = str(payload.get('id', ''))

            def do_return():
                order = request.env['sale.order'].sudo().search([
                    ('shopify_order_id', '=', order_id)
                ], limit=1)
                if not order:
                    _logger.warning(f"Order not found for return: {order_id}")
                    return
                order.sudo().write({'shopify_return_id': shopify_return_id})
                return_line_items = payload.get('return_line_items', [])
                self._create_return_picking(order, return_line_items, config)
                _logger.info(f"Return requested for order {order.name}: {shopify_return_id}")

            self._safe_execute(do_return)
            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"returns/create webhook error: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/returns/update',
                type='http', auth='public', csrf=False, methods=['POST'])
    def returns_update(self, **kwargs):
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')
            shop_domain = request.httprequest.headers.get('X-Shopify-Shop-Domain', '')
            config = self._get_config_for_shop(shop_domain)
            if not self._verify_webhook(data, hmac_header, config):
                return request.make_response('Unauthorized', status=401)
            payload = json.loads(data)
            order_id = str(payload.get('order_id', ''))
            status = payload.get('status', '')
            order = request.env['sale.order'].sudo().search([
                ('shopify_order_id', '=', order_id)
            ], limit=1)
            if order:
                _logger.info(f"Return status updated for {order.name}: {status}")
            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"returns/update webhook error: {e}")
            return request.make_response('OK', status=200)

    @http.route('/shopify/webhooks/refunds/create',
                type='http', auth='public', csrf=False, methods=['POST'])
    def refunds_create(self, **kwargs):
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')
            shop_domain = request.httprequest.headers.get('X-Shopify-Shop-Domain', '')
            config = self._get_config_for_shop(shop_domain)
            if not self._verify_webhook(data, hmac_header, config):
                return request.make_response('Unauthorized', status=401)
            if not config:
                return request.make_response('OK', status=200)
            payload = json.loads(data)
            order_id = str(payload.get('order_id', ''))
            refund_line_items = payload.get('refund_line_items', [])

            def do_refund():
                order = request.env['sale.order'].sudo().search([
                    ('shopify_order_id', '=', order_id)
                ], limit=1)
                if not order:
                    _logger.warning(f"Order not found for refund: {order_id}")
                    return

                order.sudo().write({'shopify_financial_status': 'refunded'})

                existing_return_pickings = order.picking_ids.filtered(
                    lambda p: p.picking_type_code == 'incoming' and p.state == 'done'
                )

                if existing_return_pickings:
                    _logger.info(f"Return for {order.name} already processed via Odoo — skipping return picking")
                else:
                    if refund_line_items:
                        return_picking = self._create_and_validate_return_picking(
                            order, refund_line_items, config
                        )
                        if return_picking:
                            _logger.info(f"Return picking created and validated for {order.name}: {return_picking.name}")

                existing_refunds = order.invoice_ids.filtered(
                    lambda i: i.move_type == 'out_refund'
                )
                if not existing_refunds:
                    importer = request.env['shopify.order.import'].sudo()
                    importer._process_refund(order, config)
                    _logger.info(f"Refund processed for order {order.name}")
                else:
                    _logger.info(f"Refund received for {order.name} — credit note already exists")

            self._safe_execute(do_refund)
            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"refunds/create webhook error: {e}")
            return request.make_response('OK', status=200)

    def _create_return_picking(self, order, return_line_items, config, auto_validate=False):
        """Create a return picking in Odoo based on Shopify return request."""
        try:
            original_picking = order.picking_ids.filtered(
                lambda p: p.state == 'done' and p.picking_type_code == 'outgoing'
            )
            if not original_picking:
                order.message_post(
                    body="⚠️ Return requested via Shopify but no completed delivery found. Please process manually.",
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

            result = return_wizard.action_create_returns()
            return_picking_id = result.get('res_id')

            if return_picking_id:
                return_picking = request.env['stock.picking'].sudo().browse(return_picking_id)

                if auto_validate:
                    for move in return_picking.move_ids:
                        move.quantity = move.product_uom_qty
                    return_picking.with_context(skip_immediate=True).button_validate()
                    _logger.info(f"Return picking validated for {order.name}: {return_picking.name}")
                else:
                    order.message_post(
                        body=f"📦 Return requested via Shopify — return picking created: {return_picking.name}",
                        message_type='comment',
                        subtype_xmlid='mail.mt_note',
                    )
                    _logger.info(f"Return picking created for {order.name}: {return_picking.name}")

                return return_picking

        except Exception as e:
            _logger.error(f"Return picking creation failed for {order.name}: {e}")
            order.message_post(
                body=f"⚠️ Return requested via Shopify but return picking creation failed: {e}. Please process manually.",
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )
            return None

    def _create_and_validate_return_picking(self, order, refund_line_items, config):
        """Create and immediately validate return picking for flow 1 (Shopify self-service)."""
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
