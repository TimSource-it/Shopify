from odoo import models, fields, api
from odoo.exceptions import UserError


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    shopify_product_id = fields.Char(
        string='Shopify Product ID',
        copy=False,
    )
    shopify_last_sync = fields.Datetime(
        string='Last Shopify Sync',
        copy=False,
    )
    shopify_sync_status = fields.Selection([
        ('not_synced', 'Not synced'),
        ('synced', 'Synced'),
        ('error', 'Error'),
        ('pending', 'Queued'),
    ], string='Shopify Sync Status', default='not_synced', copy=False)
    shopify_sync_error = fields.Char(
        string='Shopify Sync Error',
        copy=False,
    )
    shopify_published = fields.Boolean(
        string='Publish on Shopify',
        default=False,
        help='If checked, this product will be synchronized to Shopify.',
    )
    shopify_tags = fields.Char(
        string='Shopify Tags',
        help='Comma-separated tags for Shopify. E.g: new, sale, featured',
        copy=False,
    )
    shopify_description = fields.Html(
        string='Shopify Description',
        help='Specific description for Shopify. Empty = website description or sales description.',
        copy=False,
    )
    shopify_inventory_html = fields.Html(
        string='Shopify Inventory',
        compute='_compute_shopify_inventory_html',
        sanitize=False,
    )

    def write(self, vals):
        result = super().write(vals)
        sync_fields = {
            'name', 'description_sale', 'shopify_description',
            'list_price', 'shopify_tags', 'categ_id',
            'image_1920', 'weight', 'active',
        }
        if sync_fields.intersection(vals.keys()):
            pending_products = self.filtered(
                lambda p: p.shopify_published and p.shopify_sync_status != 'pending'
            )
            if pending_products:
                pending_products.with_context(no_sync_trigger=True).write({
                    'shopify_sync_status': 'pending'
                })
        return result

    def _get_shopify_inventory_lines(self):
        """Get inventory per Shopify location mapping."""
        result = []
        config = self.env['shopify.config'].search([
            ('state', '=', 'connected')
        ], limit=1)
        if not config:
            return result

        location_mappings = self.env['shopify.location'].search([
            ('config_id', '=', config.id),
            ('sync_inventory', '=', True),
            ('warehouse_id', '!=', False),
        ])

        for mapping in location_mappings:
            qty = 0
            for variant in self.product_variant_ids:
                location = mapping.warehouse_id.lot_stock_id
                quants = self.env['stock.quant'].search([
                    ('product_id', '=', variant.id),
                    ('location_id', '=', location.id),
                ])
                qty += sum(quants.mapped('available_quantity'))
            result.append({
                'location_name': mapping.shopify_location_name,
                'warehouse_name': mapping.warehouse_id.name,
                'qty': max(0, int(qty)),
            })
        return result

    @api.depends('product_variant_ids', 'shopify_published')
    def _compute_shopify_inventory_html(self):
        for product in self:
            lines = product._get_shopify_inventory_lines()
            if not lines:
                product.shopify_inventory_html = '<p>No location mapping configured.</p>'
                continue

            html = '<table style="width:100%; border-collapse:collapse;">'
            html += '<tr style="background:#f5f5f5; font-weight:bold;">'
            html += '<td style="padding:8px; border:1px solid #ddd;">Shopify Location</td>'
            html += '<td style="padding:8px; border:1px solid #ddd;">Odoo Warehouse</td>'
            html += '<td style="padding:8px; border:1px solid #ddd;">Available Stock</td>'
            html += '</tr>'
            for line in lines:
                html += '<tr>'
                html += f'<td style="padding:8px; border:1px solid #ddd;">{line["location_name"]}</td>'
                html += f'<td style="padding:8px; border:1px solid #ddd;">{line["warehouse_name"]}</td>'
                html += f'<td style="padding:8px; border:1px solid #ddd;">{line["qty"]}</td>'
                html += '</tr>'
            html += '</table>'
            product.shopify_inventory_html = html

    def action_sync_to_shopify(self):
        """Sync this product to Shopify."""
        self.ensure_one()

        if not self.shopify_published and not self.shopify_product_id:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Not synced',
                    'message': "Enable 'Publish on Shopify' to synchronize this product.",
                    'type': 'warning',
                    'sticky': False,
                }
            }

        result = self.env['shopify.sync'].sync_product_to_shopify(self.id)
        if result:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Synced!',
                    'message': f"{self.name} has been successfully synchronized to Shopify.",
                    'type': 'success',
                    'sticky': False,
                }
            }
        else:
            raise UserError("Sync failed. Please check the sync status for details.")

    def action_bulk_sync_to_shopify(self):
        """Sync multiple products to Shopify."""
        success = 0
        failed = 0
        skipped = 0
        for product in self:
            if not product.shopify_published and not product.shopify_product_id:
                skipped += 1
                continue
            result = self.env['shopify.sync'].sync_product_to_shopify(product.id)
            if result:
                success += 1
            else:
                failed += 1

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Bulk sync completed',
                'message': f"{success} synced, {failed} failed, {skipped} skipped.",
                'type': 'success' if failed == 0 else 'warning',
                'sticky': True,
            }
        }


class ProductProduct(models.Model):
    _inherit = 'product.product'

    shopify_variant_id = fields.Char(
        string='Shopify Variant ID',
        copy=False,
    )
    shopify_inventory_item_id = fields.Char(
        string='Shopify Inventory Item ID',
        copy=False,
    )
