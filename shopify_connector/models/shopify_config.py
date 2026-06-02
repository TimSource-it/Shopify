from odoo import models, fields, api
from odoo.exceptions import UserError
import requests
import secrets
import logging
from datetime import datetime, timedelta

_logger = logging.getLogger(__name__)


class ShopifyConfig(models.Model):
    _name = 'shopify.config'
    _description = 'Shopify Connection Settings'
    _rec_name = 'shop_name'

    shop_name = fields.Char(
        string='Shop Name',
        required=True,
        help='E.g. my-store (without .myshopify.com)'
    )
    shop_url = fields.Char(
        string='Shop URL',
        compute='_compute_shop_url',
        store=True,
    )
    access_token = fields.Char(string='Admin API Access Token')
    client_id = fields.Char(string='Client ID')
    client_secret = fields.Char(string='Client Secret')
    refresh_token = fields.Char(string='Refresh Token')
    access_token_expires_at = fields.Datetime(string='Token Expires At')
    active = fields.Boolean(string='Active', default=True)
    state = fields.Selection([
        ('draft', 'Not Connected'),
        ('connected', 'Connected'),
        ('error', 'Error'),
    ], string='Status', default='draft')

    pricelist_id = fields.Many2one(
        'product.pricelist',
        string='Shopify Pricelist',
        help='Which pricelist is used for prices sent to Shopify. Empty = default sales price.',
    )
    shopify_location_id = fields.Char(
        string='Default Shopify Location ID',
        help='Fallback location if no mapping is available.',
    )
    shopify_carrier_service_id = fields.Char(
        string='Shopify CarrierService ID',
        readonly=True,
    )
    location_ids = fields.One2many(
        'shopify.location',
        'config_id',
        string='Locations',
    )
    carrier_mapping_ids = fields.One2many(
        'shopify.carrier.mapping',
        'config_id',
        string='Shipping Method Mappings',
    )
    published_scope = fields.Selection([
        ('web', 'Web only'),
        ('global', 'All channels'),
    ], string='Sales Channel', default='global')

    sync_products = fields.Boolean(string='Sync Products', default=True)
    sync_orders = fields.Boolean(string='Import Orders', default=True)
    sync_inventory = fields.Boolean(string='Sync Inventory', default=True)
    sync_customers = fields.Boolean(string='Sync Customers', default=True)
    allow_backorder = fields.Boolean(string='Allow Backorder', default=False)

    confirm_order_on = fields.Selection([
        ('paid', 'Paid only'),
        ('authorized', 'Paid or authorized'),
        ('always', 'Always confirm'),
        ('never', 'Never auto-confirm'),
    ], string='Confirm Order On', default='paid')

    invoice_policy = fields.Selection([
        ('on_confirm', 'On confirmation'),
        ('on_delivery', 'On delivery'),
        ('never', 'Never automatically'),
    ], string='Create Invoice', default='on_confirm')

    refund_policy = fields.Selection([
        ('credit_note', 'Create credit note'),
        ('cancel', 'Cancel order'),
        ('manual', 'Process manually'),
    ], string='Return Policy', default='credit_note')

    account_id = fields.Many2one(
        'account.account',
        string='Shopify Intermediary Account',
        help='Ledger account for Shopify payments.',
    )
    tax_id = fields.Many2one(
        'account.tax',
        string='Default Tax',
        help='Default tax for Shopify orders.',
    )

    last_order_sync = fields.Datetime(string='Last Order Sync')
    last_product_sync = fields.Datetime(string='Last Product Sync')
    last_inventory_sync = fields.Datetime(string='Last Inventory Sync')

    def _account_available(self):
        return 'account.account' in self.env

    @api.depends('shop_name')
    def _compute_shop_url(self):
        for rec in self:
            if rec.shop_name:
                rec.shop_url = f"https://{rec.shop_name}.myshopify.com"
            else:
                rec.shop_url = False

    def _graphql(self, query, variables=None):
        """Execute a GraphQL query or mutation."""
        self.ensure_one()
        url = f"{self.shop_url}/admin/api/2026-04/graphql.json"
        payload = {'query': query}
        if variables:
            payload['variables'] = variables
        try:
            response = requests.post(
                url,
                json=payload,
                headers=self._get_headers(),
                timeout=15
            )
            if response.status_code == 200:
                data = response.json()
                if 'errors' in data:
                    _logger.error(f"GraphQL errors: {data['errors']}")
                    return None
                return data.get('data')
            else:
                _logger.error(f"GraphQL request failed ({response.status_code}): {response.text[:200]}")
                return None
        except Exception as e:
            _logger.error(f"GraphQL request error: {e}")
            return None

    def action_set_accounting_defaults(self):
        self.ensure_one()
        vals = {}

        if not self.account_id and self._account_available():
            existing = self.env['account.account'].search([
                ('name', '=', 'Shopify Payments'),
            ], limit=1)
            if not existing:
                try:
                    existing = self.env['account.account'].create({
                        'name': 'Shopify Payments',
                        'code': '13000',
                        'account_type': 'asset_current',
                    })
                except Exception as e:
                    _logger.error(f"Failed to create intermediary account: {e}")
            if existing:
                vals['account_id'] = existing.id

        if not self.tax_id and self._account_available():
            tax = self.env['account.tax'].search([
                ('type_tax_use', '=', 'sale'),
                ('active', '=', True),
            ], limit=1)
            if tax:
                vals['tax_id'] = tax.id

        if vals:
            self.write(vals)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Defaults loaded!',
                    'message': 'Accounting defaults have been set automatically.',
                    'type': 'success',
                    'sticky': False,
                }
            }
        else:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'No changes',
                    'message': 'Settings were already filled in or could not be created.',
                    'type': 'warning',
                    'sticky': False,
                }
            }

    def _fetch_locations(self):
        """Fetch all active Shopify locations via GraphQL."""
        try:
            query = """
            {
              locations(first: 50) {
                edges {
                  node {
                    id
                    name
                    isActive
                    legacyResourceId
                  }
                }
              }
            }
            """
            data = self._graphql(query)
            if not data:
                return

            locations = [
                edge['node']
                for edge in data.get('locations', {}).get('edges', [])
                if edge['node'].get('isActive')
            ]

            default_warehouse = self.env['stock.warehouse'].search([], limit=1)

            for i, location in enumerate(locations):
                legacy_id = location.get('legacyResourceId')
                existing = self.env['shopify.location'].search([
                    ('config_id', '=', self.id),
                    ('shopify_location_id', '=', str(legacy_id)),
                ], limit=1)
                if not existing:
                    self.env['shopify.location'].create({
                        'config_id': self.id,
                        'shopify_location_id': str(legacy_id),
                        'shopify_location_name': location.get('name', ''),
                        'warehouse_id': default_warehouse.id if (default_warehouse and i == 0) else False,
                        'sync_inventory': i == 0,
                    })
                    _logger.info(f"Location created: {location.get('name')}")

            if locations:
                self.shopify_location_id = str(locations[0].get('legacyResourceId'))

        except Exception as e:
            _logger.error(f"Failed to fetch locations: {e}")

    def _fetch_location_id(self):
        self._fetch_locations()

    def _get_or_create_shipping_product(self):
        """Get or create the default shipping product."""
        product = self.env['product.product'].search([
            ('default_code', '=', 'SHOPIFY-SHIPPING')
        ], limit=1)
        if not product:
            product = self.env['product.product'].create({
                'name': 'Shopify Shipping',
                'default_code': 'SHOPIFY-SHIPPING',
                'type': 'service',
                'invoice_policy': 'order',
            })
        return product

    def action_import_shipping_methods(self):
        """Import shipping methods from Shopify and create carrier mappings."""
        self.ensure_one()

        query = """
        {
          deliveryProfiles(first: 5) {
            edges {
              node {
                name
                profileLocationGroups {
                  locationGroupZones(first: 10) {
                    edges {
                      node {
                        zone {
                          name
                        }
                        methodDefinitions(first: 10) {
                          edges {
                            node {
                              name
                              active
                              rateProvider {
                                __typename
                                ... on DeliveryRateDefinition {
                                  price {
                                    amount
                                    currencyCode
                                  }
                                }
                                ... on DeliveryParticipant {
                                  fixedFee {
                                    amount
                                    currencyCode
                                  }
                                  participantServices {
                                    active
                                    name
                                  }
                                }
                              }
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """

        data = self._graphql(query)
        if not data:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Error',
                    'message': 'Failed to fetch shipping methods.',
                    'type': 'danger',
                    'sticky': False,
                }
            }

        imported = 0
        skipped = 0
        method_names_seen = set()
        shipping_product = self._get_or_create_shipping_product()

        for profile_edge in data.get('deliveryProfiles', {}).get('edges', []):
            profile = profile_edge['node']
            for location_group in profile.get('profileLocationGroups', []):
                for zone_edge in location_group.get('locationGroupZones', {}).get('edges', []):
                    zone_node = zone_edge['node']
                    zone_name = zone_node.get('zone', {}).get('name', '')

                    for method_edge in zone_node.get('methodDefinitions', {}).get('edges', []):
                        method = method_edge['node']
                        method_name = method.get('name', '')

                        if not method_name or not method.get('active'):
                            continue

                        unique_name = method_name
                        if method_name in method_names_seen:
                            unique_name = f"{method_name} ({zone_name})"
                        method_names_seen.add(method_name)

                        existing_mapping = self.env['shopify.carrier.mapping'].search([
                            ('config_id', '=', self.id),
                            ('shopify_method_name', '=', unique_name),
                        ], limit=1)

                        if existing_mapping:
                            skipped += 1
                            continue

                        rate_provider = method.get('rateProvider', {})
                        provider_type = rate_provider.get('__typename', '')

                        if provider_type == 'DeliveryRateDefinition':
                            fixed_price = float(rate_provider.get('price', {}).get('amount', 0))
                        else:
                            fixed_price = 0.0

                        carrier = self.env['delivery.carrier'].search([
                            ('name', '=', unique_name),
                            ('active', '=', True),
                        ], limit=1)

                        if not carrier:
                            carrier = self.env['delivery.carrier'].create({
                                'name': unique_name,
                                'delivery_type': 'fixed',
                                'fixed_price': fixed_price,
                                'product_id': shipping_product.id,
                            })
                            _logger.info(f"Carrier created: {unique_name} (€{fixed_price})")

                        self.env['shopify.carrier.mapping'].create({
                            'config_id': self.id,
                            'shopify_method_name': unique_name,
                            'carrier_id': carrier.id,
                        })
                        imported += 1
                        _logger.info(f"Shipping method linked: {unique_name} → {carrier.name}")

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Shipping methods imported',
                'message': f"{imported} new methods imported, {skipped} already present.",
                'type': 'success',
                'sticky': False,
            }
        }

    def _register_webhooks(self):
        """Register webhooks with Shopify via GraphQL."""
        base_url = self._get_base_url()

        webhook_topics = [
            ('APP_UNINSTALLED', f"{base_url}/shopify/webhooks/app/uninstalled"),
            ('ORDERS_CREATE', f"{base_url}/shopify/webhooks/orders/create"),
            ('ORDERS_UPDATED', f"{base_url}/shopify/webhooks/orders/updated"),
            ('ORDERS_CANCELLED', f"{base_url}/shopify/webhooks/orders/cancelled"),
            ('RETURNS_REQUEST', f"{base_url}/shopify/webhooks/returns/create"),
            ('RETURNS_UPDATE', f"{base_url}/shopify/webhooks/returns/update"),
            ('REFUNDS_CREATE', f"{base_url}/shopify/webhooks/refunds/create"),
        ]

        existing_query = """
        {
          webhookSubscriptions(first: 50) {
            edges {
              node {
                id
                topic
                endpoint {
                  ... on WebhookHttpEndpoint {
                    callbackUrl
                  }
                }
              }
            }
          }
        }
        """
        data = self._graphql(existing_query)
        existing_webhooks = {}
        if data:
            for edge in data.get('webhookSubscriptions', {}).get('edges', []):
                node = edge['node']
                callback = node.get('endpoint', {}).get('callbackUrl', '')
                existing_webhooks[callback] = node['id']

        create_mutation = """
        mutation webhookSubscriptionCreate($topic: WebhookSubscriptionTopic!, $callbackUrl: URL!) {
          webhookSubscriptionCreate(
            topic: $topic
            webhookSubscription: {
              callbackUrl: $callbackUrl
              format: JSON
            }
          ) {
            webhookSubscription {
              id
              topic
            }
            userErrors {
              field
              message
            }
          }
        }
        """

        for topic, callback_url in webhook_topics:
            if callback_url in existing_webhooks:
                _logger.info(f"Webhook already registered: {topic}")
                continue
            try:
                result = self._graphql(create_mutation, {
                    'topic': topic,
                    'callbackUrl': callback_url,
                })
                if result:
                    errors = result.get('webhookSubscriptionCreate', {}).get('userErrors', [])
                    if errors:
                        _logger.warning(f"Webhook registration error for {topic}: {errors}")
                    else:
                        _logger.info(f"Webhook registered via GraphQL: {topic}")
            except Exception as e:
                _logger.error(f"Webhook registration error: {e}")

    def _register_carrier_service(self):
        """Register our app as a CarrierService with Shopify."""
        try:
            base_url = self._get_base_url()
            callback_url = f"{base_url}/shopify/carrier/rates"

            url = f"{self.shop_url}/admin/api/2026-04/carrier_services.json"
            response = requests.get(url, headers=self._get_headers(), timeout=10)

            if response.status_code == 200:
                existing = response.json().get('carrier_services', [])
                for cs in existing:
                    if cs.get('callback_url') == callback_url:
                        _logger.info(f"CarrierService already registered for {self.shop_name}")
                        return True

            response = requests.post(
                url,
                json={
                    'carrier_service': {
                        'name': 'Odoo Connector by Source IT',
                        'callback_url': callback_url,
                        'service_discovery': True,
                        'format': 'json',
                    }
                },
                headers=self._get_headers(),
                timeout=10
            )

            if response.status_code in (200, 201):
                carrier_service_id = response.json().get('carrier_service', {}).get('id')
                self.write({'shopify_carrier_service_id': str(carrier_service_id)})
                _logger.info(f"CarrierService registered for {self.shop_name}: {carrier_service_id}")
                return True
            else:
                error_data = response.json()
                error_msg = error_data.get('errors', {})
                if 'base' in error_msg:
                    error_text = error_msg['base'][0] if error_msg['base'] else str(error_msg)
                else:
                    error_text = str(error_msg)

                _logger.warning(
                    f"CarrierService not available for {self.shop_name}: {error_text}. "
                    f"Configure shipping methods manually in Shopify."
                )
                return False

        except Exception as e:
            _logger.error(f"CarrierService registration error: {e}")
            return False

    def _unregister_carrier_service(self):
        """Remove our CarrierService from Shopify."""
        try:
            if not self.shopify_carrier_service_id:
                return True

            url = f"{self.shop_url}/admin/api/2026-04/carrier_services/{self.shopify_carrier_service_id}.json"
            response = requests.delete(url, headers=self._get_headers(), timeout=10)

            if response.status_code in (200, 204):
                self.write({'shopify_carrier_service_id': False})
                _logger.info(f"CarrierService removed for {self.shop_name}")
                return True
            else:
                _logger.warning(f"Failed to remove CarrierService: {response.text[:200]}")
                return False

        except Exception as e:
            _logger.error(f"CarrierService removal error: {e}")
            return False

    def action_fetch_locations(self):
        self.ensure_one()
        self._fetch_locations()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'shopify.config',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _get_valid_token(self):
        if self.access_token_expires_at:
            if datetime.utcnow() >= self.access_token_expires_at - timedelta(minutes=5):
                _logger.info(f"Token expired for {self.shop_name}, refreshing...")
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
            'read_merchant_managed_fulfillment_orders',
            'write_merchant_managed_fulfillment_orders',
            'read_assigned_fulfillment_orders',
            'write_assigned_fulfillment_orders',
            'read_third_party_fulfillment_orders',
            'write_third_party_fulfillment_orders',
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
            raise UserError("Please fill in the Client ID first.")
        if not self.shop_name:
            raise UserError("Please fill in the shop name first.")
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
                self._fetch_locations()
                self._register_webhooks()
                self._register_carrier_service()
                return True
            else:
                self.state = 'error'
                _logger.error(f"Token exchange failed: {response.text}")
                return False
        except Exception as e:
            _logger.error(f"Token exchange error: {e}")
            self.state = 'error'
            return False

    def _refresh_access_token(self):
        self.ensure_one()
        if not self.refresh_token:
            _logger.error(f"No refresh token for {self.shop_name}")
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
                vals = {'access_token': token_data.get('access_token')}
                if token_data.get('refresh_token'):
                    vals['refresh_token'] = token_data['refresh_token']
                if token_data.get('expires_in'):
                    vals['access_token_expires_at'] = datetime.utcnow() + timedelta(seconds=token_data['expires_in'])
                self.write(vals)
                _logger.info(f"Token refreshed for {self.shop_name}")
                return True
            else:
                _logger.error(f"Token refresh failed: {response.text}")
                self.state = 'error'
                return False
        except Exception as e:
            _logger.error(f"Token refresh error: {e}")
            return False

    def action_test_connection(self):
        self.ensure_one()
        if not self.access_token:
            raise UserError("No access token. Please connect to Shopify first.")
        try:
            query = "{ shop { name } }"
            data = self._graphql(query)
            if data:
                shop_name = data.get('shop', {}).get('name', self.shop_name)
                self.state = 'connected'
                if not self.location_ids:
                    self._fetch_locations()
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Connection successful!',
                        'message': f"Connected to: {shop_name}",
                        'type': 'success',
                        'sticky': False,
                    }
                }
            else:
                self.state = 'error'
                raise UserError("Connection failed.")
        except requests.exceptions.ConnectionError:
            self.state = 'error'
            raise UserError("Could not establish connection.")
        except requests.exceptions.Timeout:
            self.state = 'error'
            raise UserError("Connection timed out.")

    def action_import_orders(self):
        self.ensure_one()
        imported = self.env['shopify.order.import'].import_orders_from_shopify(self)
        if imported is not False:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Import successful!',
                    'message': f"{imported} orders imported.",
                    'type': 'success',
                    'sticky': False,
                }
            }
        else:
            raise UserError("Failed to import orders.")
