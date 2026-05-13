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
    access_token = fields.Char(
        string='Admin API Access Token',
    )
    client_id = fields.Char(
        string='Client ID',
        help='Shopify App Client ID uit het Dev Dashboard'
    )
    client_secret = fields.Char(
        string='Client Secret',
        help='Shopify App Client Secret uit het Dev Dashboard'
    )
    active = fields.Boolean(
        string='Actief',
        default=True,
    )
    state = fields.Selection([
        ('draft', 'Niet verbonden'),
        ('connected', 'Verbonden'),
        ('error', 'Fout'),
    ], string='Status', default='draft')

    sync_products = fields.Boolean(string='Producten synchroniseren', default=True)
    sync_orders = fields.Boolean(string='Bestellingen importeren', default=True)
    sync_inventory = fields.Boolean(string='Voorraad synchroniseren', default=True)
    sync_customers = fields.Boolean(string='Klanten synchroniseren', default=True)
    allow_backorder = fields.Boolean(
        string='Bestellen bij 0 voorraad',
        default=False,
    )

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

    def action_get_token(self):
        """Haal access token op via Client Credentials flow."""
        self.ensure_one()
        if not self.client_id or not self.client_secret:
            raise UserError("Vul eerst de Client ID en Client Secret in.")
        try:
            url = f"https://{self.shop_name}.myshopify.com/admin/oauth/access_token"
            data = {
                'client_id': self.client_id,
                'client_secret': self.client_secret,
                'grant_type': 'client_credentials',
            }
            response = requests.post(url, json=data, timeout=10)
            if response.status_code == 200:
                token_data = response.json()
                self.write({
                    'access_token': token_data.get('access_token'),
                    'state': 'connected',
                })
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Verbinding geslaagd!',
                        'message': f"Token ontvangen voor {self.shop_name}",
                        'type': 'success',
                        'sticky': False,
                    }
                }
            else:
                self.state = 'error'
                raise UserError(f"Token ophalen mislukt: {response.text}")
        except requests.exceptions.ConnectionError:
            self.state = 'error'
            raise UserError("Kan geen verbinding maken met Shopify.")

    def action_test_connection(self):
        """Test de verbinding met de Shopify winkel."""
        self.ensure_one()
        if not self.access_token:
            raise UserError("Geen access token. Klik eerst op Verbind met Shopify.")
        try:
            url = f"{self.shop_url}/admin/api/2026-04/shop.json"
            response = requests.get(url, headers=self._get_headers(), timeout=10)
            if response.status_code == 200:
                shop_data = response.json().get('shop', {})
                self.state = 'connected'
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Verbinding geslaagd!',
                        'message': f"Verbonden met: {shop_data.get('name', self.shop_name)}",
                        'type': 'success',
                        'sticky': False,
                    }
                }
            else:
                self.state = 'error'
                raise UserError(f"Verbinding mislukt (status {response.status_code}).")
        except requests.exceptions.ConnectionError:
            self.state = 'error'
            raise UserError("Kan geen verbinding maken.")
        except requests.exceptions.Timeout:
            self.state = 'error'
            raise UserError("Verbinding time-out.")
