from odoo import models, fields, api
import requests
import logging

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
        """Haal de actieve Shopify config op."""
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

            # Bouw tracking URL op
            tracking_url = ''
            if carrier and carrier.tracking_url and tracking_number:
                tracking_url = carrier.tracking_url.replace(
                    '<shipmenttrackingnumber>', tracking_number
                )

            # Haal fulfillment orders op via GraphQL
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

            # Maak fulfillment aan via GraphQL mutation
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
        # Controleer of dit een retour is van een Shopify order
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
            # Credit nota aanmaken op basis van config instelling
            if config.refund_policy == 'credit_note' and config._account_available():
                self._create_return_credit_note(sale_order, config)
            elif config.refund_policy == 'cancel':
                if sale_order.state not in ('done', 'cancel'):
                    sale_order.action_cancel()
                    _logger.info(f"Order {sale_order.name} geannuleerd wegens retour")

            # Shopify bijwerken
            self._update_shopify_return_status(sale_order, config)

        except Exception as e:
            _logger.error(f"Retour verwerking fout voor {sale_order.name}: {e}")

    def _create_return_credit_note(self, sale_order, config):
        """Maak een credit nota aan voor geretourneerde producten."""
        try:
            # Bepaal welke producten en hoeveel zijn teruggekomen
            return_lines = {}
            for move in self.move_ids:
                product = move.product_id
                qty = move.quantity
                if qty > 0:
                    return_lines[product.id] = return_lines.get(product.id, 0) + qty

            if not return_lines:
                return

            # Zoek de originele factuur
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

            # Maak credit nota aan voor de meest recente factuur
            invoice = invoices.sorted('invoice_date', reverse=True)[0]

            # Gebruik Odoo's ingebouwde credit nota functionaliteit
            credit_note_wizard = self.env['account.move.reversal'].create({
                'move_ids': [(4, invoice.id)],
                'reason': f"Retour voor {sale_order.name}",
                'journal_id': invoice.journal_id.id,
            })
            result = credit_note_wizard.reverse_moves()

            # Haal de aangemaakte credit nota op
            credit_note_id = result.get('res_id')
            if credit_note_id:
                credit_note = self.env['account.move'].browse(credit_note_id)

                # Pas de hoeveelheden aan op basis van wat er teruggekomen is
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
        """Update de Shopify order status na retour verwerking via GraphQL."""
        try:
            shopify_order_id = sale_order.shopify_order_id

            # Haal de Shopify return ID op als die bekend is
            shopify_return_id = getattr(sale_order, 'shopify_return_id', False)

            if shopify_return_id:
                # Sluit de return in Shopify
                mutation = """
                mutation returnClose($id: ID!) {
                  returnClose(id: $id) {
                    return {
                      id
                      status
                    }
                    userErrors {
                      field
                      message
                    }
                  }
                }
                """
                result = config._graphql(mutation, {'id': shopify_return_id})
                if result:
                    errors = result.get('returnClose', {}).get('userErrors', [])
                    if errors:
                        _logger.warning(f"Shopify return sluiten mislukt: {errors}")
                    else:
                        _logger.info(f"Shopify return gesloten voor {sale_order.name}")

            # Update fulfillment status op de order
            sale_order.write({'shopify_fulfillment_status': 'returned'})

        except Exception as e:
            _logger.error(f"Shopify return status update fout: {e}")


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
