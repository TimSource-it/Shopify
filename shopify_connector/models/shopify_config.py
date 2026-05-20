from odoo import models, fields, api
from odoo.exceptions import UserError
import requests
import secrets
import logging
from datetime import datetime, timedelta

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
    refresh_token = fields.Char(string='Refresh Token')
    access_token_expires_at = fields.Datetime(string='Token verloopt op')
    active = fields.Boolean(string='Actief', default=True)
    state = fields.Selection([
        ('draft', 'Niet verbonden'),
        ('connected', 'Verbonden'),
        ('error', 'Fout'),
    ], string='Status', default='draft')

    pricelist_id = fields.Many2one(
        'product.pricelist',
        string='Prijslijst voor Shopify',
        help='Welke prijslijst wordt gebruikt voor de prijs naar Shopify. Leeg = standaard verkoopprijs.',
    )
    shopify_location_id = fields.Char(
        string='Shopify Locatie ID',
        help='Wordt automatisch opgehaald bij verbinding.',
    )
    published_scope = fields.Selection([
        ('web', 'Alleen webshop'),
        ('global', 'Alle kanalen'),
    ], string='Verkoopkanaal', default='global',
        help='Bepaalt op welke verkoopkanalen producten zichtbaar zijn in Shopify.')

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

    def _fetch_location_id(self):
        """Haal de eerste actieve Shopify locatie op."""
        try:
            url = f"{self.shop_url}/admin/api/2025-01/locations.json"
            response = requests.get(url, headers=self._get_headers(), timeout=10)
            if response.status_code == 200:
                locations = response.json().get('locations', [])
                active = [l for l in locations if l.get('active')]
                if active:
                    self.shopify_location_id = str(active[0]['id'])
                    _logger.info(f"Locatie ID opgehaald: {self.shopify_location_id}")
                    return self.shopify_location_id
        except Exception as e:
            _logger.error(f"Locatie ophalen mislukt: {e}")
        return False

    def _get_valid_token(self):
        """Geeft een geldig access token terug, vernieuwt indien nodig."""
        if self.access_token_expires_at:
            if datetime.utcnow() >= self.access_token_expires_at - timedelta(minutes=5):
                _logger.info(f"Token verlopen voor {self.shop_name}, vernieuwen...")
                self._refresh_access_token()
        return self.access_token

    def _get_headers(self):
        token = self._get_valid_token()
        return {
            'X-Shopify-Access-Token': token,
            'Content-Type': 'application/json',
        }

    def _get_base_url(self):
        return self.env['ir.config_parameter'].sudo().get_param('web.base.url')

    def _build_oauth_url(self, shop, state=None):
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
        state_param = state or secrets.token_hex(32)
        return (
            f"https://{shop}/admin/oauth/authorize"
            f"?client_id={self.client_id}"
            f"&scope={scopes}"
            f"&redirect_uri={redirect_uri}"
            f"&state={state_param}"
        )

    def action_start_oauth(self):
        self.ensure_one()
        if not self.client_id:
            raise UserError("Vul eerst de Client ID in.")
        if not self.shop_name:
            raise UserError("Vul eerst de winkelnaam in.")
        shop = f"{self.shop_name}.myshopify.com"
        state = self.env['shopify.oauth.state'].create_state(self.shop_name)
        oauth_url = self._build_oauth_url(shop, state)
        return {
            'type': 'ir.actions.act_url',
            'url': oauth_url,
            'target': 'self',
        }

    def _exchange_code_for_token(self, code, shop):
        self.ensure_one()
        try:
            url = f"https://{shop}/admin/oauth/access_token"
            response = requests.post(
                url,
                data={
                    'client_id': self.client_id,
                    'client_secret': self.client_secret,
                    'code': code,
                    'expiring': '1',
                },
                timeout=10
            )
            _logger.info(f"Token exchange response: {response.status_code} - {response.text}")
            if response.status_code == 200:
                token_data = response.json()
                vals = {
                    'access_token': token_data.get('access_token'),
                    'state': 'connected',
                }
                if token_data.get('refresh_token'):
                    vals['refresh_token'] = token_data['refresh_token']
                if token_data.get('expires_in'):
                    vals['access_token_expires_at'] = datetime.utcnow() + timedelta(seconds=token_data['expires_in'])
                self.write(vals)
                self._fetch_location_id()
                return True
            else:
                self.state = 'error'
                _logger.error(f"Token exchange mislukt: {response.text}")
                return False
        except Exception as e:
            _logger.error(f"Token exchange fout: {e}")
            self.state = 'error'
            return False

    def _refresh_access_token(self):
        """Vernieuwt het access token met het refresh token."""
        self.ensure_one()
        if not self.refresh_token:
            _logger.error(f"Geen refresh token voor {self.shop_name}")
            return False
        try:
            url = f"{self.shop_url}/admin/oauth/access_token"
            response = requests.post(
                url,
                data={
                    'client_id': self.client_id,
                    'client_secret': self.client_secret,
                    'grant_type': 'refresh_token',
                    'refresh_token': self.refresh_token,
                },
                timeout=10
            )
            if response.status_code == 200:
                token_data = response.json()
                vals = {
                    'access_token': token_data.get('access_token'),
                }
                if token_data.get('refresh_token'):
                    vals['refresh_token'] = token_data['refresh_token']
                if token_data.get('expires_in'):
                    vals['access_token_expires_at'] = datetime.utcnow() + timedelta(seconds=token_data['expires_in'])
                self.write(vals)
                _logger.info(f"Token vernieuwd voor {self.shop_name}")
                return True
            else:
                _logger.error(f"Token refresh mislukt: {response.text}")
                self.state = 'error'
                return False
        except Exception as e:
            _logger.error(f"Token refresh fout: {e}")
            return False

    def action_test_connection(self):
        self.ensure_one()
        if not self.access_token:
            raise UserError("Geen access token. Klik eerst op Verbind met Shopify.")
        try:
            url = f"{self.shop_url}/admin/api/2025-01/shop.json"
            response = requests.get(url, headers=self._get_headers(), timeout=10)
            if response.status_code == 200:
                shop_data = response.json().get('shop', {})
                self.state = 'connected'
                if not self.shopify_location_id:
                    self._fetch_location_id()
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
