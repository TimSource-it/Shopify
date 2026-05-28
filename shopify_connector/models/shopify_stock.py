from odoo import models, fields, api
import requests
import logging

_logger = logging.getLogger(__name__)


class StockQuant(models.Model):
    _inherit = 'stock.quant'

    def write(self, vals):
        result = super().write(vals)
        if 'quantity' in vals or 'reserved_quantity' in vals:
            try:
                products = self.mapped('product_id.product_tmpl_id')
                for product in products.filtered(
                    lambda p: p.shopify_product_id and p.shopify_published
                ):
                    product.with_context(no_sync_trigger=True).write({
                        'shopify_sync_status': 'pending'
                    })
            except Exception as e:
                _logger.error(f"Voorraad pending zetten mislukt: {e}")
        return result


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def button_validate(self):
        result = super().button_validate()
        try:
            # Voorraad pending zetten
            products = self.move_ids.mapped('product_id.product_tmpl_id')
            for product in products.filtered(
                lambda p: p.shopify_product_id and p.shopify_published
            ):
                product.with_context(no_sync_trigger=True).write({
                    'shopify_sync_status': 'pending'
                })
        except Exception as e:
            _logger.error(f"Voorraad pending zetten na levering mislukt: {e}")

        # Fulfillment aanmaken in Shopify
        try:
            self._create_shopify_fulfillment()
        except Exception as e:
            _logger.error(f"Shopify fulfillment aanmaken mislukt: {e}")

        return result

    def _create_shopify_fulfillment(self):
        """Maak een fulfillment aan in Shopify na levering validatie."""
        # Alleen uitgaande leveringen
        if self.picking_type_code != 'outgoing':
            return

        # Zoek de gekoppelde sale order
        sale_order = self.sale_id
        if not sale_order:
            return

        # Controleer of het een Shopify order is
        if not sale_order.shopify_order_id:
            return

        # Haal de config op
        config = self.env['shopify.config'].search([
            ('state', '=', 'connected'),
        ], limit=1)
        if not config:
            return

        try:
            # Haal tracking informatie op
            tracking_number = self.carrier_tracking_ref or ''
            carrier = self.carrier_id

            # Bouw tracking URL op
            tracking_url = ''
            if carrier and carrier.tracking_url and tracking_number:
                tracking_url = carrier.tracking_url.replace(
                    '<shipmenttrackingnumber>', tracking_number
                )

            # Haal de line item IDs op uit Shopify
            shopify_order_id = sale_order.shopify_order_id
            order_url = f"{config.shop_url}/admin/api/2025-01/orders/{shopify_order_id}.json"
            order_response = requests.get(
                order_url,
                headers=config._get_headers(),
                timeout=15
            )

            if order_response.status_code != 200:
                _logger.error(f"Shopify order ophalen mislukt: {order_response.text[:200]}")
                return

            shopify_order = order_response.json().get('order', {})
            line_items = shopify_order.get('line_items', [])

            # Bouw fulfillment line items op
            fulfillment_line_items = []
            for move in self.move_ids:
                product = move.product_id
                # Zoek het bijbehorende Shopify line item
                for line_item in line_items:
                    if str(line_item.get('variant_id', '')) == str(product.shopify_variant_id or ''):
                        fulfillment_line_items.append({
                            'id': line_item['id'],
                            'quantity': int(move.quantity),
                        })
                        break

            if not fulfillment_line_items:
                # Geen specifieke items gevonden — fulfileer alle items
                fulfillment_line_items = [
                    {'id': item['id'], 'quantity': item['quantity']}
                    for item in line_items
                ]

            # Bouw fulfillment data op
            fulfillment_data = {
                'fulfillment': {
                    'line_items_by_fulfillment_order': [],
                    'notify_customer': True,
                }
            }

            # Haal fulfillment orders op
            fo_url = f"{config.shop_url}/admin/api/2025-01/orders/{shopify_order_id}/fulfillment_orders.json"
            fo_response = requests.get(fo_url, headers=config._get_headers(), timeout=15)

            if fo_response.status_code == 200:
                fulfillment_orders = fo_response.json().get('fulfillment_orders', [])
                open_fos = [
                    fo for fo in fulfillment_orders
                    if fo.get('status') in ('open', 'in_progress')
                ]

                for fo in open_fos:
                    fo_entry = {'fulfillment_order_id': fo['id']}
                    fulfillment_data['fulfillment']['line_items_by_fulfillment_order'].append(fo_entry)

            if not fulfillment_data['fulfillment']['line_items_by_fulfillment_order']:
                _logger.warning(f"Geen open fulfillment orders gevonden voor {shopify_order_id}")
                return

            # Voeg tracking toe als beschikbaar
            if tracking_number:
                fulfillment_data['fulfillment']['tracking_info'] = {
                    'number': tracking_number,
                    'url': tracking_url,
                    'company': carrier.name if carrier else '',
                }

            # Maak fulfillment aan
            url = f"{config.shop_url}/admin/api/2025-01/fulfillments.json"
            response = requests.post(
                url,
                json=fulfillment_data,
                headers=config._get_headers(),
                timeout=15
            )

            if response.status_code in (200, 201):
                fulfillment = response.json().get('fulfillment', {})
                _logger.info(
                    f"Shopify fulfillment aangemaakt voor order {sale_order.name}: "
                    f"fulfillment ID {fulfillment.get('id')}"
                )
                # Update fulfillment status op de order
                sale_order.write({
                    'shopify_fulfillment_status': 'fulfilled',
                })
            else:
                _logger.error(
                    f"Shopify fulfillment mislukt voor {sale_order.name}: "
                    f"{response.text[:200]}"
                )

        except Exception as e:
            _logger.error(f"Shopify fulfillment fout voor {sale_order.name}: {e}")


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    def action_confirm(self):
        result = super().action_confirm()
        self._sync_inventory_direct()
        return result

    def action_cancel(self):
        result = super().action_cancel()
        self._sync_inventory_direct()
        return result

    def _sync_inventory_direct(self):
        """Sync voorraad direct naar Shopify zonder pending."""
        try:
            config = self.env['shopify.config'].search([
                ('state', '=', 'connected'),
                ('sync_inventory', '=', True),
            ], limit=1)
            if config:
                products = self.order_line.mapped('product_id.product_tmpl_id')
                for product in products.filtered(
                    lambda p: p.shopify_product_id and p.shopify_published
                ):
                    self.env['shopify.sync'].sync_inventory_to_shopify(product.id, config)
        except Exception as e:
            _logger.error(f"Directe voorraad sync mislukt: {e}")
