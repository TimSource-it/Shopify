from odoo import models, fields, api
import requests
import logging

_logger = logging.getLogger(__name__)


class ShopifyOrderImport(models.AbstractModel):
    _name = 'shopify.order.import'
    _description = 'Shopify Bestelling Import'

    @api.model
    def _get_config(self, shop_name=None):
        domain = [('state', '=', 'connected')]
        if shop_name:
            domain.append(('shop_name', '=', shop_name))
        return self.env['shopify.config'].search(domain, limit=1)

    @api.model
    def _get_or_create_partner(self, customer_data, shipping_address=None):
        """Zoek of maak een klant aan in Odoo."""
        if not customer_data:
            return self.env['res.partner'].browse(1)  # Gebruik standaard klant

        email = customer_data.get('email', '')
        name = f"{customer_data.get('first_name', '')} {customer_data.get('last_name', '')}".strip()
        shopify_customer_id = str(customer_data.get('id', ''))

        # Zoek op Shopify klant ID
        partner = self.env['res.partner'].search([
            ('shopify_customer_id', '=', shopify_customer_id)
        ], limit=1)

        if not partner and email:
            # Zoek op email
            partner = self.env['res.partner'].search([
                ('email', '=', email)
            ], limit=1)

        if not partner:
            # Maak nieuwe klant aan
            vals = {
                'name': name or email or 'Shopify Klant',
                'email': email,
                'shopify_customer_id': shopify_customer_id,
                'customer_rank': 1,
            }
            # Telefoon
            phone = customer_data.get('phone', '')
            if phone:
                vals['phone'] = phone

            # Adres
            address = shipping_address or customer_data.get('default_address', {})
            if address:
                vals['street'] = address.get('address1', '')
                vals['street2'] = address.get('address2', '')
                vals['city'] = address.get('city', '')
                vals['zip'] = address.get('zip', '')
                country_code = address.get('country_code', '')
                if country_code:
                    country = self.env['res.country'].search([
                        ('code', '=', country_code)
                    ], limit=1)
                    if country:
                        vals['country_id'] = country.id

            partner = self.env['res.partner'].create(vals)
            _logger.info(f"Nieuwe klant aangemaakt: {partner.name}")
        else:
            # Update Shopify klant ID als nog niet bekend
            if not partner.shopify_customer_id:
                partner.shopify_customer_id = shopify_customer_id

        return partner

    @api.model
    def _get_product_by_variant_id(self, shopify_variant_id):
        """Zoek product op basis van Shopify variant ID."""
        if not shopify_variant_id:
            return False
        return self.env['product.product'].search([
            ('shopify_variant_id', '=', str(shopify_variant_id))
        ], limit=1)

    @api.model
    def _import_order(self, order_data, config):
        """Importeer een enkele Shopify bestelling als verkooporder."""
        shopify_order_id = str(order_data.get('id', ''))
        shopify_order_number = order_data.get('order_number', '')

        # Check of bestelling al bestaat
        existing = self.env['sale.order'].search([
            ('shopify_order_id', '=', shopify_order_id)
        ], limit=1)
        if existing:
            _logger.info(f"Bestelling {shopify_order_number} bestaat al, overgeslagen")
            return existing

        # Haal klant op
        customer_data = order_data.get('customer', {})
        shipping_address = order_data.get('shipping_address', {})
        partner = self._get_or_create_partner(customer_data, shipping_address)

        # Maak verkooporder aan
        order_vals = {
            'partner_id': partner.id,
            'shopify_order_id': shopify_order_id,
            'shopify_order_number': str(shopify_order_number),
            'shopify_financial_status': order_data.get('financial_status', ''),
            'shopify_fulfillment_status': order_data.get('fulfillment_status', '') or 'unfulfilled',
            'client_order_ref': f"Shopify #{shopify_order_number}",
        }

        order = self.env['sale.order'].create(order_vals)

        # Voeg orderregels toe
        for line in order_data.get('line_items', []):
            shopify_variant_id = line.get('variant_id')
            product = self._get_product_by_variant_id(shopify_variant_id)

            line_vals = {
                'order_id': order.id,
                'name': line.get('title', 'Onbekend product'),
                'product_uom_qty': line.get('quantity', 1),
                'price_unit': float(line.get('price', 0)),
            }

            if product:
                line_vals['product_id'] = product.id
                line_vals['name'] = product.name
            else:
                # Gebruik generiek product als variant niet gevonden
                generic = self.env['product.product'].search([
                    ('default_code', '=', 'SHOPIFY-GENERIC')
                ], limit=1)
                if not generic:
                    generic = self.env['product.product'].create({
                        'name': 'Shopify Product',
                        'default_code': 'SHOPIFY-GENERIC',
                        'type': 'service',
                    })
                line_vals['product_id'] = generic.id
                line_vals['name'] = line.get('title', 'Onbekend product')

            self.env['sale.order.line'].create(line_vals)

        _logger.info(f"Bestelling {shopify_order_number} geïmporteerd als {order.name}")
        return order

    @api.model
    def import_orders_from_shopify(self, config=None, since_id=None):
        """Importeer bestellingen van Shopify."""
        if not config:
            config = self._get_config()
        if not config:
            _logger.error("Geen actieve Shopify configuratie")
            return False

        try:
            params = 'status=any&limit=50'
            if since_id:
                params += f'&since_id={since_id}'
            elif config.last_order_sync:
                # Gebruik datum van laatste sync
                since = config.last_order_sync.strftime('%Y-%m-%dT%H:%M:%S')
                params += f'&created_at_min={since}'

            url = f"{config.shop_url}/admin/api/2025-01/orders.json?{params}"
            response = requests.get(url, headers=config._get_headers(), timeout=30)

            if response.status_code == 200:
                orders = response.json().get('orders', [])
                _logger.info(f"Shopify: {len(orders)} bestellingen gevonden")

                imported = 0
                for order_data in orders:
                    try:
                        self._import_order(order_data, config)
                        imported += 1
                    except Exception as e:
                        _logger.error(f"Bestelling import fout: {e}")

                # Update laatste sync tijd
                config.last_order_sync = fields.Datetime.now()
                _logger.info(f"{imported} bestellingen geïmporteerd")
                return imported
            else:
                _logger.error(f"Bestellingen ophalen mislukt: {response.text[:200]}")
                return False

        except Exception as e:
            _logger.error(f"Order import fout: {e}")
            return False

    @api.model
    def cron_import_orders(self):
        """Cron job: importeer nieuwe bestellingen."""
        config = self._get_config()
        if not config or not config.sync_orders:
            return
        self.import_orders_from_shopify(config)
