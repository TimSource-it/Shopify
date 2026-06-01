from odoo import models, fields, api
import logging

_logger = logging.getLogger(__name__)


class ShopifyCarrierMapping(models.Model):
    _name = 'shopify.carrier.mapping'
    _description = 'Shopify Verzendmethode Mapping'
    _rec_name = 'shopify_method_name'

    config_id = fields.Many2one(
        'shopify.config',
        string='Shopify Configuratie',
        required=True,
        ondelete='cascade',
    )
    shopify_method_name = fields.Char(
        string='Shopify Verzendmethode',
        required=True,
        help='De naam van de verzendmethode zoals die in Shopify staat, bijv. "DHL Express Worldwide"',
    )
    carrier_id = fields.Many2one(
        'delivery.carrier',
        string='Odoo Vervoerder',
        help='De vervoerder in Odoo die gekoppeld wordt aan deze verzendmethode',
    )

    _sql_constraints = [
        ('unique_method_per_config', 'UNIQUE(config_id, shopify_method_name)',
         'Deze verzendmethode is al gekoppeld aan deze configuratie.'),
    ]
