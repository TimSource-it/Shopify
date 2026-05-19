from odoo import models, fields


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
