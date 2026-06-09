from odoo import models, fields, api
from odoo.exceptions import ValidationError
import logging

_logger = logging.getLogger(__name__)


class ShopifyLocation(models.Model):
    _name = 'shopify.location'
    _description = 'Shopify Location Mapping'
    _rec_name = 'shopify_location_name'

    _sql_constraints = [
        ('unique_location_per_config',
         'UNIQUE(config_id, shopify_location_id)',
         'This Shopify location is already linked to this shop.'),
    ]

    config_id = fields.Many2one(
        'shopify.config',
        string='Shopify Shop',
        required=True,
        ondelete='cascade',
    )
    shopify_location_id = fields.Char(
        string='Shopify Location ID',
        required=True,
    )
    shopify_location_name = fields.Char(
        string='Shopify Location Name',
    )
    warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Odoo Warehouse',
        help='Link this Shopify location to an Odoo warehouse.',
    )
    sync_inventory = fields.Boolean(
        string='Sync Inventory',
        default=True,
        help='If checked, inventory from this warehouse will be synced to this location.',
    )

    @api.constrains('warehouse_id', 'config_id', 'sync_inventory')
    def _check_unique_warehouse(self):
        configs = self.mapped('config_id')
        for config in configs:
            all_locations = self.search([
                ('config_id', '=', config.id),
                ('sync_inventory', '=', True),
                ('warehouse_id', '!=', False),
            ])
            warehouse_ids = all_locations.mapped('warehouse_id').ids
            if len(warehouse_ids) != len(set(warehouse_ids)):
                raise ValidationError(
                    "Each warehouse may only be linked to one Shopify location."
                )
