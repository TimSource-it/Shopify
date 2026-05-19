from odoo import models, fields, api
from odoo.exceptions import UserError


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    shopify_product_id = fields.Char(
        string='Shopify Product ID',
        copy=False,
    )
    shopify_last_sync = fields.Datetime(
        string='Laatste Shopify sync',
        copy=False,
    )
    shopify_sync_status = fields.Selection([
        ('not_synced', 'Niet gesynchroniseerd'),
        ('synced', 'Gesynchroniseerd'),
        ('error', 'Fout'),
        ('pending', 'In wachtrij'),
    ], string='Shopify sync status', default='not_synced', copy=False)
    shopify_sync_error = fields.Char(
        string='Shopify sync fout',
        copy=False,
    )
    shopify_published = fields.Boolean(
        string='Publiceren op Shopify',
        default=False,
        help='Als dit aangevinkt is wordt het product gesynchroniseerd naar Shopify.',
    )

    def action_sync_to_shopify(self):
        """Synchroniseer dit product naar Shopify."""
        self.ensure_one()
        result = self.env['shopify.sync'].sync_product_to_shopify(self.id)
        if result:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Gesynchroniseerd!',
                    'message': f"{self.name} is succesvol gesynchroniseerd naar Shopify.",
                    'type': 'success',
                    'sticky': False,
                }
            }
        else:
            raise UserError("Synchronisatie mislukt. Controleer de sync status voor details.")

    def action_bulk_sync_to_shopify(self):
        """Synchroniseer meerdere producten naar Shopify."""
        success = 0
        failed = 0
        skipped = 0
        for product in self:
            if not product.shopify_published:
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
                'title': 'Bulk sync voltooid',
                'message': f"{success} gesynchroniseerd, {failed} mislukt, {skipped} overgeslagen.",
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
