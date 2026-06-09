from odoo import models, fields
import logging

_logger = logging.getLogger(__name__)


class ShopifyCarrierMapping(models.Model):
    _name = 'shopify.carrier.mapping'
    _description = 'Shopify Shipping Method Mapping'
    _rec_name = 'shopify_method_name'

    config_id = fields.Many2one(
        'shopify.config',
        string='Shopify Configuration',
        required=True,
        ondelete='cascade',
    )
    shopify_method_name = fields.Char(
        string='Shopify Shipping Method',
        required=True,
        help='The name of the shipping method as configured in Shopify, e.g. "DHL Express Worldwide"',
    )
    carrier_id = fields.Many2one(
        'delivery.carrier',
        string='Odoo Carrier',
        help='The carrier in Odoo linked to this shipping method',
    )

    _sql_constraints = [
        ('unique_method_per_config', 'UNIQUE(config_id, shopify_method_name)',
         'This shipping method is already linked to this configuration.'),
    ]
