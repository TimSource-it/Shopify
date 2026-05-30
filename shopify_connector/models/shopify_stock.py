from odoo import models, fields, api
import logging
import uuid

_logger = logging.getLogger(__name__)


class StockQuant(models.Model):
    _inherit = 'stock.quant'

    def write(self, vals):
        result = super().write(vals)
        if 'quantity' in vals or 'reserved_quantity' in vals:
            try:
                products = self.mapped('product_id.product_tmpl_id')
                for product in products.filtered(
                    lambda p: p.shopify_product_id and p.shopify_published
                ):
                    product.with_context(no_sync_trigger=True).write({
                        'shopify_sync_status': 'pending'
                    })
            except Exception as e:
                _logger.error(f"Voorraad pending zetten mislukt: {e}")
        return result


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def button_validate(self):
        result = super().button_validate()

        # Voorraad pending zetten
        try:
            products = self.move_ids.mapped('product_id.product_tmpl_id')
            for product in products.filtered(
                lambda p: p.shopify_product_id and p.shopify_published
            ):
                product.with_context(no_sync_trigger=True).write({
                    'shopify_sync_status': 'pending'
                })
        except Exception as e:
            _logger.error(f"Voorraad pending zetten na levering mislukt: {e}")

        # Fulfillment aanmaken of retour verwerken
        try:
            if self.picking_type_code == 'outgoing':
                self._create_shopify_fulfillment()
            elif self.picking_type_code == 'incoming':
                self._process_shopify_return()
        except Exception as e:
            _logger.error(f"Shopify verwerking mislukt na validatie: {e}")

        return result

    def _get_shopify_config(self):
        return self.env['shopify.config'].search([
            ('state', '=', 'connected'),
        ], limit=1)

    def _create_shopify_fulfillment(self):
        """Maak een fulfillment aan in Shopify na levering validatie via GraphQL."""
        sale_order = self.sale_id
        if not sale_order or not sale_order.shopify_order_id:
            return

        config = self._get_shopify_config()
        if not config:
            return

        try:
            shopify_order_id = sale_order.shopify_order_id
            tracking_number = self.carrier_tracking_ref or ''
            carrier = self.carrier_id

            tracking_url = ''
            if carrier and carrier.tracking_url and tracking_number:
                tracking_url = carrier.tracking_url.replace(
                    '<shipmenttrackingnumber>', tracking_number
                )

            query = """
            query getFulfillmentOrders($orderId: ID!) {
              order(id: $orderId) {
                fulfillmentOrders(first: 10) {
                  edges {
                    node {
                      id
                      status
                      supportedActions {
                        action
                      }
                    }
                  }
                }
              }
            }
            """
            variables = {
                'orderId': f"gid://shopify/Order/{shopify_order_id}"
            }

            data = config._graphql(query, variables)
            if not data:
                _logger.error(f"Fulfillment orders ophalen mislukt voor {sale_order.name}")
                return

            fulfillment_order_ids = []
            for edge in data.get('order', {}).get('fulfillmentOrders', {}).get('edges', []):
                node = edge['node']
                actions = [a['action'] for a in node.get('supportedActions', [])]
                if node.get('status') in ('OPEN', 'IN_PROGRESS') and 'CREATE_FULFILLMENT' in actions:
                    fulfillment_order_ids.append(node['id'])

            if not fulfillment_order_ids:
                _logger.warning(f"Geen open fulfillment orders voor {sale_order.name}")
                return

            mutation = """
            mutation fulfillmentCreateV2($fulfillment: FulfillmentV2Input!) {
              fulfillmentCreateV2(fulfillment: $fulfillment) {
                fulfillment {
                  id
                  status
                  trackingInfo {
                    number
                    url
                    company
                  }
                }
                userErrors {
                  field
                  message
                }
              }
            }
            """

            fulfillment_input = {
                'lineItemsByFulfillmentOrder': [
                    {'fulfillmentOrderId': fo_id}
                    for fo_id in fulfillment_order_ids
                ],
                'notifyCustomer': True,
            }

            if tracking_number:
                fulfillment_input['trackingInfo'] = {
                    'number': tracking_number,
                    'url': tracking_url,
                    'company': carrier.name if carrier else '',
                }

            result = config._graphql(mutation, {'fulfillment': fulfillment_input})
            if result:
                errors = result.get('fulfillmentCreateV2', {}).get('userErrors', [])
                if errors:
                    _logger.error(f"Fulfillment aanmaken fout voor {sale_order.name}: {errors}")
                else:
                    fulfillment = result.get('fulfillmentCreateV2', {}).get('fulfillment', {})
                    _logger.info(f"Shopify fulfillment aangemaakt voor {sale_order.name}: {fulfillment.get('id')}")
                    sale_order.write({'shopify_fulfillment_status': 'fulfilled'})

        except Exception as e:
            _logger.error(f"Shopify fulfillment fout voor {sale_order.name}: {e}")

    def _process_shopify_return(self):
        """Verwerk een retour in Shopify na validatie retourlevering."""
        origin_picking = self.env['stock.picking'].search([
            ('name', '=', self.origin),
        ], limit=1)

        sale_order = None
        if origin_picking and origin_picking.sale_id:
            sale_order = origin_picking.sale_id
        elif self.sale_id:
            sale_order = self.sale_id

        if not sale_order or not sale_order.shopify_order_id:
            return

        config = self._get_shopify_config()
        if not config:
            return

        try:
            if config.refund_policy == 'credit_note' and config._account_available():
                self._create_return_credit_note(sale_order, config)
            elif config.refund_policy == 'cancel':
                if sale_order.state not in ('done', 'cancel'):
                    sale_order.action_cancel()
                    _logger.info(f"Order {sale_order.name} geannuleerd wegens retour")

            self._update_shopify_return_status(sale_order, config)

        except Exception as e:
            _logger.error(f"Retour verwerking fout voor {sale_order.name}: {e}")

    def _create_return_credit_note(self, sale_order, config):
        """Maak een credit nota aan voor geretourneerde producten."""
        try:
            return_lines = {}
            for move in self.move_ids:
                product = move.product_id
                qty = move.quantity
                if qty > 0:
                    return_lines[product.id] = return_lines.get(product.id, 0) + qty

            if not return_lines:
                return

            invoices = sale_order.invoice_ids.filtered(
                lambda i: i.state == 'posted' and i.move_type == 'out_invoice'
            )

            if not invoices:
                _logger.warning(f"Geen factuur gevonden voor retour van {sale_order.name}")
                sale_order.message_post(
                    body="⚠️ Retour ontvangen maar geen factuur gevonden voor credit nota. Verwerk handmatig.",
                    message_type='comment',
                    subtype_xmlid='mail.mt_note',
                )
                return

            invoice = invoices.sorted('invoice_date', reverse=True)[0]

            credit_note_wizard = self.env['account.move.reversal'].create({
                'move_ids': [(4, invoice.id)],
                'reason': f"Retour voor {sale_order.name}",
                'journal_id': invoice.journal_id.id,
            })
            result = credit_note_wizard.reverse_moves()

            credit_note_id = result.get('res_id')
            if credit_note_id:
                credit_note = self.env['account.move'].browse(credit_note_id)

                for line in credit_note.invoice_line_ids:
                    product_id = line.product_id.id
                    if product_id in return_lines:
                        line.quantity = return_lines[product_id]
                    else:
                        line.quantity = 0

                credit_note.action_post()
                _logger.info(f"Credit nota aangemaakt voor retour van {sale_order.name}: {credit_note.name}")

                sale_order.message_post(
                    body=f"✅ Retour verwerkt — credit nota aangemaakt: {credit_note.name}",
                    message_type='comment',
                    subtype_xmlid='mail.mt_note',
                )

        except Exception as e:
            _logger.error(f"Credit nota aanmaken mislukt voor retour van {sale_order.name}: {e}")
            sale_order.message_post(
                body=f"⚠️ Retour ontvangen maar credit nota aanmaken mislukt: {e}. Verwerk handmatig.",
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    def _update_shopify_return_status(self, sale_order, config):
        """Maak een refund aan in Shopify na retour verwerking via GraphQL."""
        try:
            shopify_order_id = sale_order.shopify_order_id

            # Stap 1: haal de Shopify order op om line item IDs en transacties te krijgen
            query = """
            query getOrder($orderId: ID!) {
              order(id: $orderId) {
                id
                lineItems(first: 50) {
                  edges {
                    node {
                      id
                      quantity
                      variant {
                        id
                        legacyResourceId
                      }
                    }
                  }
                }
                transactions(first: 10) {
                  id
                  kind
                  status
                  amountSet {
                    shopMoney {
                      amount
                      currencyCode
                    }
                  }
                }
              }
            }
            """
            data = config._graphql(query, {
                'orderId': f"gid://shopify/Order/{shopify_order_id}"
            })

            if not data or not data.get('order'):
                _logger.warning(f"Shopify order niet gevonden voor {sale_order.name}")
                return

            shopify_line_items = data['order'].get('lineItems', {}).get('edges', [])
            transactions = data['order'].get('transactions', [])

            # Stap 2: bepaal welke producten zijn teruggekomen
            return_lines = {}
            for move in self.move_ids:
                if move.quantity > 0:
                    variant_id = move.product_id.shopify_variant_id
                    if variant_id:
                        return_lines[str(variant_id)] = return_lines.get(str(variant_id), 0) + int(move.quantity)

            if not return_lines:
                _logger.warning(f"Geen retour producten gevonden voor {sale_order.name}")
                return

            # Stap 3: koppel aan Shopify line items
            refund_line_items = []
            refund_amount = 0.0
            for edge in shopify_line_items:
                node = edge['node']
                variant = node.get('variant') or {}
                variant_legacy_id = str(variant.get('legacyResourceId', ''))
                if variant_legacy_id in return_lines:
                    qty = return_lines[variant_legacy_id]
                    refund_line_items.append({
                        'lineItemId': node['id'],
                        'quantity': qty,
                        'restockType': 'RETURN',
                        'locationId': f"gid://shopify/Location/{config.shopify_location_id}",
                    })
                    # Bereken terug te betalen bedrag op basis van sale order regel
                    for order_line in sale_order.order_line:
                        if order_line.product_id.shopify_variant_id == variant_legacy_id:
                            refund_amount += order_line.price_unit * qty
                            break

            if not refund_line_items:
                _logger.warning(f"Geen Shopify line items gevonden voor retour van {sale_order.name}")
                return

            # Stap 4: zoek de originele transactie voor terugbetaling
            parent_transaction_id = None
            for transaction in transactions:
                if transaction.get('kind') in ('SALE', 'CAPTURE') and transaction.get('status') == 'SUCCESS':
                    parent_transaction_id = transaction['id']
                    break

            # Stap 5: maak refund aan in Shopify
            idempotency_key = str(uuid.uuid4())
            mutation = """
            mutation refundCreate($input: RefundInput!, $idempotencyKey: String!) {
              refundCreate(input: $input) @idempotent(key: $idempotencyKey) {
                refund {
                  id
                  totalRefundedSet {
                    shopMoney {
                      amount
                      currencyCode
                    }
                  }
                }
                userErrors {
                  field
                  message
                }
              }
            }
            """

            refund_input = {
                'orderId': f"gid://shopify/Order/{shopify_order_id}",
                'refundLineItems': refund_line_items,
                'notify': True,
            }

            if parent_transaction_id and refund_amount > 0:
                refund_input['transactions'] = [{
                    'parentId': parent_transaction_id,
                    'amount': str(round(refund_amount, 2)),
                    'kind': 'REFUND',
                    'gateway': 'shopify_payments',
                }]

            result = config._graphql(mutation, {
                'input': refund_input,
                'idempotencyKey': idempotency_key,
            })

            if result:
                errors = result.get('refundCreate', {}).get('userErrors', [])
                if errors:
                    _logger.error(f"Shopify refund aanmaken mislukt voor {sale_order.name}: {errors}")
                    sale_order.message_post(
                        body=f"⚠️ Retour verwerkt in Odoo maar Shopify refund mislukt: {errors}. Verwerk handmatig in Shopify.",
                        message_type='comment',
                        subtype_xmlid='mail.mt_note',
                    )
                else:
                    refund = result.get('refundCreate', {}).get('refund', {})
                    refunded_amount = refund.get('totalRefundedSet', {}).get('shopMoney', {}).get('amount', 0)
                    _logger.info(f"Shopify refund aangemaakt voor {sale_order.name}: €{refunded_amount}")
                    sale_order.write({'shopify_fulfillment_status': 'returned'})
                    sale_order.message_post(
                        body=f"✅ Shopify refund aangemaakt: €{refunded_amount}",
                        message_type='comment',
                        subtype_xmlid='mail.mt_note',
                    )

        except Exception as e:
            _logger.error(f"Shopify return status update fout voor {sale_order.name}: {e}")


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    shopify_return_id = fields.Char(
        string='Shopify Return ID',
        readonly=True,
    )

    def action_confirm(self):
        result = super().action_confirm()
        self._sync_inventory_direct()
        return result

    def action_cancel(self):
        result = super().action_cancel()
        self._sync_inventory_direct()
        return result

    def _sync_inventory_direct(self):
        """Sync voorraad direct naar Shopify zonder pending."""
        try:
            config = self.env['shopify.config'].search([
                ('state', '=', 'connected'),
                ('sync_inventory', '=', True),
            ], limit=1)
            if config:
                products = self.order_line.mapped('product_id.product_tmpl_id')
                for product in products.filtered(
                    lambda p: p.shopify_product_id and p.shopify_published
                ):
                    self.env['shopify.sync'].sync_inventory_to_shopify(product.id, config)
        except Exception as e:
            _logger.error(f"Directe voorraad sync mislukt: {e}")
