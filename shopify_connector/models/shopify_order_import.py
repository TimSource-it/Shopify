from odoo import models, fields, api
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
    def _should_confirm_order(self, config, financial_status):
        if config.confirm_order_on == 'always':
            return True
        if config.confirm_order_on == 'never':
            return False
        if config.confirm_order_on == 'paid':
            return financial_status == 'paid'
        if config.confirm_order_on == 'authorized':
            return financial_status in ('paid', 'authorized')
        return False

    @api.model
    def _create_invoice(self, order, config):
        if not config._account_available():
            return False
        if config.invoice_policy == 'never':
            return False
        try:
            invoice = order._create_invoices()
            if not invoice:
                return False
            if config.account_id:
                for line in invoice.invoice_line_ids:
                    line.account_id = config.account_id
            invoice.action_post()
            if config.account_id:
                self._register_payment(invoice, config)
            _logger.info(f"Factuur aangemaakt voor order {order.name}")
            return invoice
        except Exception as e:
            _logger.error(f"Factuur aanmaken mislukt voor {order.name}: {e}")
            return False

    @api.model
    def _register_payment(self, invoice, config):
        try:
            if not config._account_available():
                return
            payment_method = self.env['account.payment.method.line'].search([
                ('payment_type', '=', 'inbound'),
                ('journal_id.type', 'in', ['bank', 'cash']),
            ], limit=1)
            if not payment_method:
                return
            payment_vals = {
                'amount': invoice.amount_total,
                'payment_type': 'inbound',
                'partner_type': 'customer',
                'partner_id': invoice.partner_id.id,
                'journal_id': payment_method.journal_id.id,
                'payment_method_line_id': payment_method.id,
                'memo': invoice.name,
            }
            payment = self.env['account.payment'].create(payment_vals)
            payment.action_post()
            lines = payment.line_ids.filtered(
                lambda l: l.account_id == invoice.account_id
            )
            invoice.js_assign_outstanding_line(lines.id)
            _logger.info(f"Betaling geregistreerd voor {invoice.name}")
        except Exception as e:
            _logger.error(f"Betaling registreren mislukt: {e}")

    @api.model
    def _get_or_create_partner(self, customer_data, shipping_address=None):
        if not customer_data:
            return self.env['res.partner'].browse(1)

        email = customer_data.get('email', '')
        name = f"{customer_data.get('first_name', '')} {customer_data.get('last_name', '')}".strip()
        shopify_customer_id = str(customer_data.get('id', ''))

        self.env.cr.execute(
            "SELECT id FROM res_partner WHERE shopify_customer_id = %s FOR UPDATE SKIP LOCKED",
            (shopify_customer_id,)
        )

        partner = self.env['res.partner'].search([
            ('shopify_customer_id', '=', shopify_customer_id)
        ], limit=1)

        if not partner and email:
            partner = self.env['res.partner'].search([
                ('email', '=', email)
            ], limit=1)

        if not partner:
            self.env.cr.execute(
                "SELECT id FROM res_partner WHERE shopify_customer_id = %s",
                (shopify_customer_id,)
            )
            row = self.env.cr.fetchone()
            if row:
                return self.env['res.partner'].browse(row[0])

            vals = {
                'name': name or email or 'Shopify Klant',
                'email': email,
                'shopify_customer_id': shopify_customer_id,
                'customer_rank': 1,
            }
            phone = customer_data.get('phone', '')
            if phone:
                vals['phone'] = phone
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
            try:
                partner = self.env['res.partner'].create(vals)
                _logger.info(f"Nieuwe klant aangemaakt: {partner.name}")
            except Exception as e:
                _logger.warning(f"Klant aanmaak mislukt, opnieuw zoeken: {e}")
                partner = self.env['res.partner'].search([
                    ('shopify_customer_id', '=', shopify_customer_id)
                ], limit=1)
                if not partner:
                    partner = self.env['res.partner'].browse(1)
        else:
            if not partner.shopify_customer_id:
                partner.shopify_customer_id = shopify_customer_id

        return partner

    @api.model
    def _get_product_by_variant_id(self, shopify_variant_id):
        if not shopify_variant_id:
            return False
        return self.env['product.product'].search([
            ('shopify_variant_id', '=', str(shopify_variant_id))
        ], limit=1)

    @api.model
    def _get_or_create_shipping_product(self):
        product = self.env['product.product'].search([
            ('default_code', '=', 'SHOPIFY-SHIPPING')
        ], limit=1)
        if not product:
            product = self.env['product.product'].create({
                'name': 'Shopify Verzendkosten',
                'default_code': 'SHOPIFY-SHIPPING',
                'type': 'service',
                'invoice_policy': 'order',
            })
        return product

    @api.model
    def _get_or_create_discount_product(self):
        product = self.env['product.product'].search([
            ('default_code', '=', 'SHOPIFY-DISCOUNT')
        ], limit=1)
        if not product:
            product = self.env['product.product'].create({
                'name': 'Shopify Korting',
                'default_code': 'SHOPIFY-DISCOUNT',
                'type': 'service',
                'invoice_policy': 'order',
            })
        return product

    @api.model
    def _get_tax_ids(self, product, config):
        if product and product.taxes_id:
            return [(6, 0, product.taxes_id.ids)]
        if config.tax_id and config._account_available():
            return [(6, 0, [config.tax_id.id])]
        return []

    @api.model
    def _add_shipping_lines(self, order, order_data, config):
        shipping_lines = order_data.get('shipping_lines', [])
        if not shipping_lines:
            return

        shipping_product = self._get_or_create_shipping_product()

        for shipping in shipping_lines:
            price = float(shipping.get('price', 0))
            title = shipping.get('title', 'Verzendkosten')

            carrier = self.env['delivery.carrier'].search([
                ('name', 'ilike', title),
                ('active', '=', True),
            ], limit=1)

            if carrier and not order.carrier_id and order.state == 'draft':
                try:
                    order.write({'carrier_id': carrier.id})
                except Exception as e:
                    _logger.warning(f"Carrier koppelen mislukt voor {order.name}: {e}")

            if price > 0:
                self.env['sale.order.line'].create({
                    'order_id': order.id,
                    'product_id': shipping_product.id,
                    'name': title,
                    'product_uom_qty': 1,
                    'price_unit': price,
                    'tax_ids': [(5, 0, 0)],
                })

    @api.model
    def _add_discount_lines(self, order, order_data, config):
        total_discounts = float(order_data.get('total_discounts', 0))
        if total_discounts <= 0:
            return
        discount_product = self._get_or_create_discount_product()
        discount_codes = order_data.get('discount_codes', [])
        codes = ', '.join([d.get('code', '') for d in discount_codes]) if discount_codes else 'Korting'
        self.env['sale.order.line'].create({
            'order_id': order.id,
            'product_id': discount_product.id,
            'name': f"Korting: {codes}",
            'product_uom_qty': 1,
            'price_unit': -total_discounts,
            'tax_ids': [(5, 0, 0)],
        })

    @api.model
    def _import_order(self, order_data, config):
        shopify_order_id = str(order_data.get('id', ''))
        shopify_order_number = order_data.get('order_number', '')
        financial_status = order_data.get('financial_status', '')
        fulfillment_status = order_data.get('fulfillment_status', '') or 'unfulfilled'

        self.env.cr.execute(
            "SELECT id FROM sale_order WHERE shopify_order_id = %s FOR UPDATE SKIP LOCKED",
            (shopify_order_id,)
        )
        locked_row = self.env.cr.fetchone()

        existing = self.env['sale.order'].search([
            ('shopify_order_id', '=', shopify_order_id)
        ], limit=1)

        if existing:
            existing.write({
                'shopify_financial_status': financial_status,
                'shopify_fulfillment_status': fulfillment_status,
            })
            if existing.state == 'draft' and self._should_confirm_order(config, financial_status):
                existing.action_confirm()
                if config.invoice_policy == 'on_confirm':
                    self._create_invoice(existing, config)
            return existing

        if locked_row is None and not existing:
            self.env.cr.execute(
                "SELECT id FROM sale_order WHERE shopify_order_id = %s",
                (shopify_order_id,)
            )
            if self.env.cr.fetchone():
                return False

        customer_data = order_data.get('customer', {})
        shipping_address = order_data.get('shipping_address', {})
        partner = self._get_or_create_partner(customer_data, shipping_address)

        order = self.env['sale.order'].create({
            'partner_id': partner.id,
            'shopify_order_id': shopify_order_id,
            'shopify_order_number': str(shopify_order_number),
            'shopify_financial_status': financial_status,
            'shopify_fulfillment_status': fulfillment_status,
            'client_order_ref': f"Shopify #{shopify_order_number}",
            'pricelist_id': config.pricelist_id.id if config.pricelist_id else False,
        })

        warnings = []

        for line in order_data.get('line_items', []):
            shopify_variant_id = line.get('variant_id')
            shopify_price = float(line.get('price', 0))
            product = self._get_product_by_variant_id(shopify_variant_id)

            line_vals = {
                'order_id': order.id,
                'name': line.get('title', 'Onbekend product'),
                'product_uom_qty': line.get('quantity', 1),
                'price_unit': shopify_price,
            }

            if product:
                line_vals['product_id'] = product.id
                line_vals['name'] = product.name
                odoo_price = self.env['shopify.sync']._get_price(product, config)
                if round(odoo_price, 2) != round(shopify_price, 2):
                    warnings.append(
                        f"⚠️ Prijsafwijking op '{product.name}': "
                        f"Shopify €{shopify_price:.2f} — Odoo €{odoo_price:.2f}"
                    )
            else:
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
                warnings.append(
                    f"⚠️ Niet-gekoppeld product: '{line.get('title')}' "
                    f"(Shopify variant ID: {shopify_variant_id})"
                )

            tax_ids = self._get_tax_ids(product, config)
            if tax_ids:
                line_vals['tax_ids'] = tax_ids

            self.env['sale.order.line'].create(line_vals)

        if warnings:
            order.message_post(
                body="<br/>".join(warnings),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

        self._add_shipping_lines(order, order_data, config)
        self._add_discount_lines(order, order_data, config)

        if self._should_confirm_order(config, financial_status):
            order.action_confirm()
            if config.invoice_policy == 'on_confirm':
                self._create_invoice(order, config)

        _logger.info(f"Bestelling {shopify_order_number} geïmporteerd als {order.name}")
        return order

    @api.model
    def _process_refund(self, order, config):
        if config.refund_policy == 'manual':
            return
        if config.refund_policy == 'cancel':
            if order.state not in ('done', 'cancel'):
                order.action_cancel()
            return
        if config.refund_policy == 'credit_note':
            if not config._account_available():
                return
            try:
                invoices = order.invoice_ids.filtered(
                    lambda i: i.state == 'posted' and i.move_type == 'out_invoice'
                )
                for invoice in invoices:
                    refund = invoice._reverse_moves()
                    refund.action_post()
                    _logger.info(f"Credit nota aangemaakt voor {invoice.name}")
            except Exception as e:
                _logger.error(f"Credit nota aanmaken mislukt: {e}")

    @api.model
    def import_orders_from_shopify(self, config=None, since_id=None):
        if not config:
            config = self._get_config()
        if not config:
            _logger.error("Geen actieve Shopify configuratie")
            return False

        try:
            after_cursor = None
            if config.last_order_sync:
                since = config.last_order_sync.strftime('%Y-%m-%dT%H:%M:%SZ')
                query_filter = f'created_at:>"{since}"'
            else:
                query_filter = ''

            imported = 0
            has_next_page = True

            while has_next_page:
                query = """
                query getOrders($first: Int!, $after: String, $query: String) {
                  orders(first: $first, after: $after, query: $query, sortKey: CREATED_AT) {
                    pageInfo {
                      hasNextPage
                      endCursor
                    }
                    edges {
                      node {
                        id
                        legacyResourceId
                        name
                        displayFinancialStatus
                        displayFulfillmentStatus
                        createdAt
                        customer {
                          id
                          legacyResourceId
                          email
                          firstName
                          lastName
                          phone
                          defaultAddress {
                            address1
                            address2
                            city
                            zip
                            countryCodeV2
                          }
                        }
                        shippingAddress {
                          address1
                          address2
                          city
                          zip
                          countryCodeV2
                        }
                        lineItems(first: 50) {
                          edges {
                            node {
                              id
                              title
                              quantity
                              originalUnitPriceSet {
                                shopMoney {
                                  amount
                                }
                              }
                              variant {
                                id
                                legacyResourceId
                              }
                            }
                          }
                        }
                        shippingLines(first: 10) {
                          edges {
                            node {
                              title
                              originalPriceSet {
                                shopMoney {
                                  amount
                                }
                              }
                            }
                          }
                        }
                        discountCodes
                        totalDiscountsSet {
                          shopMoney {
                            amount
                          }
                        }
                      }
                    }
                  }
                }
                """

                variables = {
                    'first': 50,
                    'after': after_cursor,
                    'query': query_filter or None,
                }

                data = config._graphql(query, variables)
                if not data:
                    break

                orders_data = data.get('orders', {})
                page_info = orders_data.get('pageInfo', {})
                has_next_page = page_info.get('hasNextPage', False)
                after_cursor = page_info.get('endCursor')

                edges = orders_data.get('edges', [])
                _logger.info(f"Shopify: {len(edges)} bestellingen gevonden")

                for edge in edges:
                    try:
                        order_node = edge['node']
                        order_data = self._normalize_graphql_order(order_node)
                        self._import_order(order_data, config)
                        imported += 1
                    except Exception as e:
                        _logger.error(f"Bestelling import fout: {e}")

            config.last_order_sync = fields.Datetime.now()
            _logger.info(f"{imported} bestellingen geïmporteerd")
            return imported

        except Exception as e:
            _logger.error(f"Order import fout: {e}")
            return False

    @api.model
    def _normalize_graphql_order(self, node):
        financial_status_map = {
            'PAID': 'paid',
            'PENDING': 'pending',
            'AUTHORIZED': 'authorized',
            'PARTIALLY_PAID': 'partially_paid',
            'PARTIALLY_REFUNDED': 'partially_refunded',
            'REFUNDED': 'refunded',
            'VOIDED': 'voided',
        }
        fulfillment_status_map = {
            'FULFILLED': 'fulfilled',
            'UNFULFILLED': 'unfulfilled',
            'PARTIALLY_FULFILLED': 'partial',
            'IN_PROGRESS': 'in_progress',
            'ON_HOLD': 'on_hold',
            'SCHEDULED': 'scheduled',
        }

        financial_status = financial_status_map.get(
            node.get('displayFinancialStatus', ''), 'pending'
        )
        fulfillment_status = fulfillment_status_map.get(
            node.get('displayFulfillmentStatus', ''), 'unfulfilled'
        )

        customer = node.get('customer') or {}
        customer_data = {}
        if customer:
            customer_data = {
                'id': customer.get('legacyResourceId', ''),
                'email': customer.get('email', ''),
                'first_name': customer.get('firstName', ''),
                'last_name': customer.get('lastName', ''),
                'phone': customer.get('phone', ''),
                'default_address': self._normalize_address(
                    customer.get('defaultAddress') or {}
                ),
            }

        shipping_addr = node.get('shippingAddress') or {}
        shipping_address = self._normalize_address(shipping_addr)

        line_items = []
        for edge in node.get('lineItems', {}).get('edges', []):
            item = edge['node']
            variant = item.get('variant') or {}
            price = item.get('originalUnitPriceSet', {}).get('shopMoney', {}).get('amount', '0')
            line_items.append({
                'title': item.get('title', ''),
                'quantity': item.get('quantity', 1),
                'price': price,
                'variant_id': variant.get('legacyResourceId', ''),
            })

        shipping_lines = []
        for edge in node.get('shippingLines', {}).get('edges', []):
            shipping = edge['node']
            price = shipping.get('originalPriceSet', {}).get('shopMoney', {}).get('amount', '0')
            shipping_lines.append({
                'title': shipping.get('title', 'Verzendkosten'),
                'price': price,
            })

        total_discounts = node.get('totalDiscountsSet', {}).get('shopMoney', {}).get('amount', '0')
        discount_codes = [
            {'code': code}
            for code in node.get('discountCodes', [])
        ]

        order_number = node.get('name', '').replace('#', '')

        return {
            'id': node.get('legacyResourceId', ''),
            'order_number': order_number,
            'financial_status': financial_status,
            'fulfillment_status': fulfillment_status,
            'customer': customer_data,
            'shipping_address': shipping_address,
            'line_items': line_items,
            'shipping_lines': shipping_lines,
            'total_discounts': total_discounts,
            'discount_codes': discount_codes,
        }

    @api.model
    def _normalize_address(self, addr):
        if not addr:
            return {}
        return {
            'address1': addr.get('address1', ''),
            'address2': addr.get('address2', ''),
            'city': addr.get('city', ''),
            'zip': addr.get('zip', ''),
            'country_code': addr.get('countryCodeV2', ''),
        }

    @api.model
    def cron_import_orders(self):
        config = self._get_config()
        if not config or not config.sync_orders:
            return
        self.import_orders_from_shopify(config)
