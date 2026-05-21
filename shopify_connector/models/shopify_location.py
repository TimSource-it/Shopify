from odoo import models, fields, api
from odoo.exceptions import ValidationError
import requests
import logging

_logger = logging.getLogger(__name__)


class ShopifyLocation(models.Model):
    _name = 'shopify.location'
    _description = 'Shopify Locatie Mapping'
    _rec_name = 'shopify_location_name'

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
    active = fields.Boolean(
        string='Synchroniseren',
        default=True,
        help='Als aangevinkt wordt de voorraad van dit magazijn naar deze locatie gesynchroniseerd.',
    )

    @api.constrains('warehouse_id', 'config_id', 'active')
    def _check_unique_warehouse(self):
        for record in self:
            if not record.warehouse_id or not record.active:
                continue
            duplicate = self.search([
                ('config_id', '=', record.config_id.id),
                ('warehouse_id', '=', record.warehouse_id.id),
                ('active', '=', True),
                ('id', '!=', record.id),
            ])
            if duplicate:
                raise ValidationError(
                    f"Magazijn '{record.warehouse_id.name}' is al gekoppeld aan "
                    f"locatie '{duplicate.shopify_location_name}'. "
                    f"Elk magazijn mag maar aan één Shopify locatie gekoppeld zijn."
                )
