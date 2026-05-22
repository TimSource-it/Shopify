from odoo import models, fields, api
from odoo.exceptions import ValidationError
import logging

_logger = logging.getLogger(__name__)


class ShopifyLocation(models.Model):
    _name = 'shopify.location'
    _description = 'Shopify Locatie Mapping'
    _rec_name = 'shopify_location_name'

    _sql_constraints = [
        ('unique_location_per_config',
         'UNIQUE(config_id, shopify_location_id)',
         'Deze Shopify locatie is al gekoppeld aan deze winkel.'),
    ]

    config_id = fields.Many2one(
        'shopify.config',
        string='Shopify Winkel',
        required=True,
        ondelete='cascade',
    )
    shopify_location_id = fields.Char(
        string='Shopify Locatie ID',
        required=True,
    )
    shopify_location_name = fields.Char(
        string='Shopify Locatie Naam',
    )
    warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Odoo Magazijn',
        help='Koppel deze Shopify locatie aan een Odoo magazijn.',
    )
    sync_inventory = fields.Boolean(
        string='Synchroniseren',
        default=True,
        help='Als aangevinkt wordt de voorraad van dit magazijn naar deze locatie gesynchroniseerd.',
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
                    "Elk magazijn mag maar aan één Shopify locatie gekoppeld zijn."
                )
