from odoo import models, fields, api
from odoo.exceptions import UserError
import requests
import json
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
        required=True,
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

    # Synchronisatie instellingen
    sync_products = fields.Boolean(string='Producten synchroniseren', default=True)
    sync_orders = fields.Boolean(string='Bestellingen importeren', default=True)
    sync_inventory = fields.Boolean(string='Voorraad synchroniseren', default=True)
    sync_customers = fields.Boolean(string='Klanten synchroniseren', default=True)
    allow_backorder = fields.Boolean(
        string='Bestellen bij 0 voorraad',
        default=False,
        help='Sta toe dat klanten bestellen als er geen voorraad is'
    )

    # Laatste sync tijden
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
        """Geeft de benodigde headers terug voor Shopify API calls."""
        return {
            'X-Shopify-Access-Token': self.access_token,
            'Content-Type': 'application/json',
        }

    def action_test_connection(self):
        """Test de verbinding met de Shopify winkel."""
        self.ensure_one()
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
                raise UserError(
                    f"Verbinding mislukt (status {response.status_code}). "
                    f"Controleer je access token en winkelnaam."
                )
        except requests.exceptions.ConnectionError:
            self.state = 'error'
            raise UserError("Kan geen verbinding maken. Controleer je internetverbinding en winkelnaam.")
        except requests.exceptions.Timeout:
            self.state = 'error'
            raise UserError("Verbinding time-out. Probeer opnieuw.")
