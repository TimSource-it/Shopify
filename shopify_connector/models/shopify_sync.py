@api.model
    def _set_inventory_graphql(self, config, inventory_item_id, location_id, qty):
        """Stel voorraad in via inventorySetQuantities (aanbevolen voor 2026-04)."""
        import uuid

        # Stap 1: haal huidige hoeveelheid op
        query = """
        query getInventoryLevel($inventoryItemId: ID!, $locationId: ID!) {
          inventoryItem(id: $inventoryItemId) {
            inventoryLevel(locationId: $locationId) {
              quantities(names: ["on_hand"]) {
                name
                quantity
              }
            }
          }
        }
        """
        current_qty = 0
        try:
            data = config._graphql(query, {
                'inventoryItemId': f"gid://shopify/InventoryItem/{inventory_item_id}",
                'locationId': f"gid://shopify/Location/{location_id}",
            })
            if data and data.get('inventoryItem') and data['inventoryItem'].get('inventoryLevel'):
                quantities = data['inventoryItem']['inventoryLevel'].get('quantities', [])
                for q in quantities:
                    if q.get('name') == 'on_hand':
                        current_qty = q.get('quantity', 0)
                        break
        except Exception as e:
            _logger.warning(f"Huidige voorraad ophalen mislukt, gebruik 0: {e}")

        # Stap 2: stel nieuwe hoeveelheid in via inventorySetQuantities
        idempotency_key = str(uuid.uuid4())
        mutation = """
        mutation inventorySetQuantities($input: InventorySetQuantitiesInput!, $idempotencyKey: String!) {
          inventorySetQuantities(input: $input) @idempotent(key: $idempotencyKey) {
            inventoryAdjustmentGroup {
              reason
              changes {
                name
                delta
                quantityAfterChange
              }
            }
            userErrors {
              code
              field
              message
            }
          }
        }
        """
        variables = {
            'input': {
                'name': 'on_hand',
                'reason': 'correction',
                'quantities': [{
                    'inventoryItemId': f"gid://shopify/InventoryItem/{inventory_item_id}",
                    'locationId': f"gid://shopify/Location/{location_id}",
                    'quantity': qty,
                    'changeFromQuantity': current_qty,
                }]
            },
            'idempotencyKey': idempotency_key,
        }
        data = config._graphql(mutation, variables)
        if data:
            errors = data.get('inventorySetQuantities', {}).get('userErrors', [])
            if errors:
                _logger.error(f"Voorraad GraphQL fout: {errors}")
                return False
            _logger.info(f"Voorraad gezet op {qty} voor item {inventory_item_id}")
            return True
        return False
