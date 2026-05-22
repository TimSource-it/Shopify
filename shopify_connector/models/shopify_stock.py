from odoo import models, fields, api
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
            products = self.move_ids.mapped('product_id.product_tmpl_id')
            for product in products.filtered(
                lambda p: p.shopify_product_id and p.shopify_published
            ):
                product.with_context(no_sync_trigger=True).write({
                    'shopify_sync_status': 'pending'
                })
        except Exception as e:
            _logger.error(f"Voorraad pending zetten na levering mislukt: {e}")
        return result


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
