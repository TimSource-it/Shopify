from odoo import models, fields, api
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
