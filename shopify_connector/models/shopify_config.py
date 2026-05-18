from odoo import models, fields, api
from odoo.exceptions import UserError
import requests
import logging

_logger = logging.getLogger(__name__)


class ShopifyConfig(models.Model):
    _name = 'shopify.config'
    _description = 'Shopify Verbindingsinstellingen'
    _rec_name = 'shop_name'

    shop_name = fields.Char(
        string='Winkelnaam',
        required=True,
        help='Bijv: mijn-winkel (zonder .myshopify.com)'
    )
    shop_url = fields.Char(
        string='Winkel URL',
        compute='_compute_shop_url',
        store=True,
    )
    access_token = fields.Char(string='Admin API Access Token')
    client_id = fields.Char(string='Client ID')
    client_secret = fields.Char(string='Client Secret')
    active = fields.Boolean(string='Actief', default=True)
    state = fields.Selection([
        ('draft', 'Niet verbonden'),
        ('connected', 'Verbonden'),
        ('error', 'Fout'),
    ], string='Status', default='draft')

    sync_products = fields.Boolean(string='Producten synchroniseren', default=True)
    sync_orders = fields.Boolean(string='Bestellingen importeren', default=True)
    sync_inventory = fields.Boolean(string='Voorraad synchroniseren', default=True)
    sync_customers = fields.Boolean(string='Klanten synchroniseren', default=True)
    allow_backorder = fields.Boolean(string='Bestellen bij 0 voorraad', default=False)

    last_order_sync = fields.Datetime(string='Laatste bestelling sync')
    last_product_sync = fields.Datetime(string='Laatste product sync')
    last_inventory_sync = fields.Datetime(string='Laatste voorraad sync')

    @api.depends('shop_name')
    def _compute_shop_url(self):
        for rec in self:
            if rec.shop_name:
                rec.shop_url = f"https://{rec.shop_name}.myshopify.com"
            else:
                rec.shop_url = False

    def _get_headers(self):
        return {
            'X-Shopify-Access-Token': self.access_token,
            'Content-Type': 'application/json',
        }

    def _get_base_url(self):
        return self.env['ir.config_parameter'].sudo().get_param('web.base.url')

    def _build_oauth_url(self, shop):
        base_url = self._get_base_url()
        redirect_uri = f"{base_url}/shopify/callback"
        scopes = ','.join([
            'read_products', 'write_products',
            'read_orders', 'write_orders',
            'read_inventory', 'write_inventory',
            'read_customers', 'write_customers',
            'read_fulfillments', 'write_fulfillments',
            'read_shipping', 'write_shipping',
            'read_returns', 'write_returns',
            'read_price_rules', 'write_price_rules',
            'read_discounts', 'write_discounts',
            'read_locations',
        ])
        return (
            f"https://{shop}/admin/oauth/authorize"
            f"?client_id={self.client_id}"
            f"&scope={scopes}"
            f"&redirect_uri={redirect_uri}"
            f"&state={self.id}"
        )

    def action_start_oauth(self):
        self.ensure_one()
        if not self.client_id:
            raise UserError("Vul eerst de Client ID in.")
        if not self.shop_name:
            raise UserError("Vul eerst de winkelnaam in.")
        shop = f"{self.shop_name}.myshopify.com"
        oauth_url = self._build_oauth_url(shop)
        return {
            'type': 'ir.actions.act_url',
            'url': oauth_url,
            'target': 'self',
