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
        string='Standaard Shopify Locatie ID',
        help='Fallback locatie als geen mapping beschikbaar is.',
    )
    shopify_carrier_service_id = fields.Char(
        string='Shopify CarrierService ID',
        readonly=True,
    )
    location_ids = fields.One2many(
        'shopify.location',
        'config_id',
        string='Locaties',
    )
    carrier_mapping_ids = fields.One2many(
        'shopify.carrier.mapping',
        'config_id',
        string='Verzendmethode Mappings',
    )
    published_scope = fields.Selection([
        ('web', 'Alleen webshop'),
        ('global', 'Alle kanalen'),
    ], string='Verkoopkanaal', default='global')

    sync_products = fields.Boolean(string='Producten synchroniseren', default=True)
    sync_orders = fields.Boolean(string='Bestellingen importeren', default=True)
    sync_inventory = fields.Boolean(string='Voorraad synchroniseren', default=True)
    sync_customers = fields.Boolean(string='Klanten synchroniseren', default=True)
    allow_backorder = fields.Boolean(string='Bestellen bij 0 voorraad', default=False)

    confirm_order_on = fields.Selection([
        ('paid', 'Alleen bij betaald'),
        ('authorized', 'Bij betaald of geautoriseerd'),
        ('always', 'Altijd bevestigen'),
        ('never', 'Nooit automatisch bevestigen'),
    ], string='Order bevestigen bij', default='paid')

    invoice_policy = fields.Selection([
        ('on_confirm', 'Bij bevestiging'),
        ('on_delivery', 'Bij levering'),
        ('never', 'Nooit automatisch'),
    ], string='Factuur aanmaken', default='on_confirm')

    refund_policy = fields.Selection([
        ('credit_note', 'Credit nota aanmaken'),
        ('cancel', 'Order annuleren'),
        ('manual', 'Handmatig verwerken'),
    ], string='Retour verwerking', default='credit_note')

    account_id = fields.Many2one(
        'account.account',
        string='Shopify Tussenrekening',
        help='Grootboekrekening voor Shopify betalingen.',
    )
    tax_id = fields.Many2one(
        'account.tax',
        string='Standaard BTW',
        help='Standaard BTW voor Shopify orders.',
    )

    last_order_sync = fields.Datetime(string='Laatste bestelling sync')
    last_product_sync = fields.Datetime(string='Laatste product sync')
    last_inventory_sync = fields.Datetime(string='Laatste voorraad sync')

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
        """Voer een GraphQL query of mutation uit."""
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
                    _logger.error(f"GraphQL fouten: {data['errors']}")
                    return None
                return data.get('data')
            else:
                _logger.error(f"GraphQL request mislukt ({response.status_code}): {response.text[:200]}")
                return None
        except Exception as e:
            _logger.error(f"GraphQL request fout: {e}")
            return None

    def action_set_accounting_defaults(self):
        self.ensure_one()
        vals = {}

        if not self.account_id and self._account_available():
            existing = self.env['account.account'].search([
                ('name', '=', 'Shopify Betalingen'),
            ], limit=1)
            if not existing:
                try:
                    existing = self.env['account.account'].create({
                        'name': 'Shopify Betalingen',
                        'code': '13000',
                        'account_type': 'asset_current',
                    })
                except Exception as e:
                    _logger.error(f"Tussenrekening aanmaken mislukt: {e}")
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
                    'title': 'Standaard instellingen ingesteld!',
                    'message': 'Boekhouding defaults zijn automatisch ingesteld.',
                    'type': 'success',
                    'sticky': False,
                }
            }
        else:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Geen wijzigingen',
                    'message': 'Instellingen waren al ingevuld of konden niet worden aangemaakt.',
                    'type': 'warning',
                    'sticky': False,
                }
            }

    def _fetch_locations(self):
        """Haal alle actieve Shopify locaties op via GraphQL."""
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
                    _logger.info(f"Locatie aangemaakt: {location.get('name')}")

            if locations:
                self.shopify_location_id = str(locations[0].get('legacyResourceId'))

        except Exception as e:
            _logger.error(f"Locaties ophalen mislukt: {e}")

    def _fetch_location_id(self):
        self._fetch_locations()

    def action_import_shipping_methods(self):
        """Importeer verzendmethodes uit Shopify en maak carrier mappings aan."""
        self.ensure_one()

        query = """
        {
          deliveryProfiles(first: 10) {
            edges {
              node {
                id
                name
                profileLocationGroups {
                  locationGroupZones(first: 50) {
                    edges {
                      node {
                        zone {
                          name
                        }
                        methodDefinitions(first: 50) {
                          edges {
                            node {
                              id
                              name
                              active
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
                    'title': 'Fout',
                    'message': 'Verzendmethodes ophalen mislukt.',
                    'type': 'danger',
                    'sticky': False,
                }
            }

        imported = 0
        skipped = 0
        method_names_seen = set()

        for profile_edge in data.get('deliveryProfiles', {}).get('edges', []):
            profile = profile_edge['node']
            for location_group in profile.get('profileLocationGroups', []):
                for zone_edge in location_group.get('locationGroupZones', {}).get('edges', []):
                    zone_node = zone_edge['node']
                    for method_edge in zone_node.get('methodDefinitions', {}).get('edges', []):
                        method = method_edge['node']
                        method_name = method.get('name', '')

                        if not method_name or not method.get('active'):
                            continue

                        # Voorkom duplicaten binnen dezelfde import
                        if method_name in method_names_seen:
                            continue
                        method_names_seen.add(method_name)

                        # Check of mapping al bestaat
                        existing = self.env['shopify.carrier.mapping'].search([
                            ('config_id', '=', self.id),
                            ('shopify_method_name', '=', method_name),
                        ], limit=1)

                        if existing:
                            skipped += 1
                            continue

                        # Zoek of er al een Odoo carrier bestaat met deze naam
                        carrier = self.env['delivery.carrier'].search([
                            ('name', 'ilike', method_name),
                            ('active', '=', True),
                        ], limit=1)

                        # Maak mapping aan
                        self.env['shopify.carrier.mapping'].create({
                            'config_id': self.id,
                            'shopify_method_name': method_name,
                            'carrier_id': carrier.id if carrier else False,
                        })
                        imported += 1
                        _logger.info(
                            f"Verzendmethode geïmporteerd: {method_name}" +
                            (f" → {carrier.name}" if carrier else " (geen carrier gevonden)")
                        )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Verzendmethodes geïmporteerd',
                'message': f"{imported} nieuwe methodes geïmporteerd, {skipped} al aanwezig.",
                'type': 'success',
                'sticky': False,
            }
        }

    def _register_webhooks(self):
        """Registreer webhooks bij Shopify via GraphQL."""
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
                _logger.info(f"Webhook al geregistreerd: {topic}")
                continue
            try:
                result = self._graphql(create_mutation, {
                    'topic': topic,
                    'callbackUrl': callback_url,
                })
                if result:
                    errors = result.get('webhookSubscriptionCreate', {}).get('userErrors', [])
                    if errors:
                        _logger.warning(f"Webhook registratie fout voor {topic}: {errors}")
                    else:
                        _logger.info(f"Webhook geregistreerd via GraphQL: {topic}")
            except Exception as e:
                _logger.error(f"Webhook registratie fout: {e}")

    def _register_carrier_service(self):
        """Registreer onze app als CarrierService bij Shopify."""
        try:
            base_url = self._get_base_url()
            callback_url = f"{base_url}/shopify/carrier/rates"

            url = f"{self.shop_url}/admin/api/2026-04/carrier_services.json"
            response = requests.get(url, headers=self._get_headers(), timeout=10)

            if response.status_code == 200:
                existing = response.json().get('carrier_services', [])
                for cs in existing:
                    if cs.get('callback_url') == callback_url:
                        _logger.info(f"CarrierService al geregistreerd voor {self.shop_name}")
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
                _logger.info(f"CarrierService geregistreerd voor {self.shop_name}: {carrier_service_id}")
                return True
            else:
                error_data = response.json()
                error_msg = error_data.get('errors', {})
                if 'base' in error_msg:
                    error_text = error_msg['base'][0] if error_msg['base'] else str(error_msg)
                else:
                    error_text = str(error_msg)

                _logger.warning(
                    f"CarrierService niet beschikbaar voor
