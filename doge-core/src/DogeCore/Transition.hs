{-# LANGUAGE DisambiguateRecordFields #-}

module DogeCore.Transition
  ( nativeTransition,
  )
where

import Data.List (find, sortOn)
import Data.Text (Text)
import Data.Text qualified as T
import DogeCore.Invariants (derivePhase)
import DogeCore.Types

-- | Native transition handlers implemented in Haskell.
nativeTransition :: PairState -> Event -> EngineConfig -> Double -> Maybe [(TradeId, Double)] -> (PairState, [Action])
nativeTransition st (EvRecoveryCancelEvent RecoveryCancelEvent {recovery_id = recoveryId, timestamp = eventTimestamp}) _ _ _ =
  (nextState, [])
  where
    withNow = st {now = eventTimestamp}
    remainingRecoveries =
      filter
        (\RecoveryOrder {recovery_id = rid} -> rid /= recoveryId)
        (recovery_orders withNow)
    nextState = clearS2FlagIfNotS2 (withNow {recovery_orders = remainingRecoveries})
nativeTransition st (EvTimerTick TimerTick {timestamp = eventTimestamp}) cfg defaultOrderSizeUsd maybeOrderSizes =
  handleTimerTick st eventTimestamp cfg defaultOrderSizeUsd maybeOrderSizes
nativeTransition st (EvPriceTick PriceTick {price = marketPrice, timestamp = eventTimestamp}) cfg defaultOrderSizeUsd maybeOrderSizes =
  handlePriceTick st marketPrice eventTimestamp cfg defaultOrderSizeUsd maybeOrderSizes
nativeTransition st (EvFillEvent fillEvent) cfg defaultOrderSizeUsd maybeOrderSizes =
  handleFillEvent st fillEvent cfg defaultOrderSizeUsd maybeOrderSizes
nativeTransition st (EvRecoveryFillEvent recoveryFillEvent) cfg _ _ =
  handleRecoveryFillEvent st recoveryFillEvent cfg

handleTimerTick :: PairState -> Double -> EngineConfig -> Double -> Maybe [(TradeId, Double)] -> (PairState, [Action])
handleTimerTick st eventTimestamp cfg defaultOrderSizeUsd maybeOrderSizes
  | sticky_mode_enabled cfg =
      ( if s2_entered_at withNow /= Nothing
          then withNow {s2_entered_at = Nothing}
          else withNow,
        []
      )
  | wouldOrphanS1Exit stAfterPhase phase cfg =
      case firstExitOrder stAfterPhase of
        Just exitOrder ->
          orphanExit stAfterPhase cfg exitOrder (T.pack "s1_timeout") defaultOrderSizeUsd maybeOrderSizes
        Nothing ->
          (stAfterPhase, [])
  | phase == S2 && wouldOrphanS2Exit stAfterPhase cfg =
      case worseS2Exit stAfterPhase of
        Just exitOrder ->
          let (stAfterOrphan, actions) =
                orphanExit stAfterPhase cfg exitOrder (T.pack "s2_timeout") defaultOrderSizeUsd maybeOrderSizes
           in (stAfterOrphan {s2_entered_at = Nothing}, actions)
        Nothing ->
          (stAfterPhase, [])
  | phase == S2 && s2_entered_at stAfterPhase == Nothing =
      (stAfterPhase {s2_entered_at = Just (now stAfterPhase)}, [])
  | otherwise =
      (stAfterPhase, [])
  where
    withNow = st {now = eventTimestamp}
    phase = derivePhase withNow
    stAfterPhase =
      if phase /= S2 && s2_entered_at withNow /= Nothing
        then withNow {s2_entered_at = Nothing}
        else withNow

orphanExit ::
  PairState ->
  EngineConfig ->
  OrderState ->
  Text ->
  Double ->
  Maybe [(TradeId, Double)] ->
  (PairState, [Action])
orphanExit st cfg order reason defaultOrderSizeUsd maybeOrderSizes =
  (stAfterFollowup, evictionActions ++ [ActOrphanOrder orphanAction] ++ followupActions)
  where
    OrderState
      { local_id = orderLocalId,
        side = orderSide,
        price = orderPrice,
        volume = orderVolume,
        trade_id = orderTradeId,
        cycle = orderCycle,
        txid = orderTxid,
        entry_price = orderEntryPrice,
        entry_fee = orderEntryFee,
        entry_filled_at = orderEntryFilledAt,
        regime_at_entry = orderRegimeAtEntry
      } = order

    maxRecoverySlots = max 1 (max_recovery_slots cfg)
    overflow = max 0 ((length (recovery_orders st) + 1) - maxRecoverySlots)
    (stAfterEvictions, evictionActions) =
      if overflow > 0 && not (null (recovery_orders st))
        then evictRecoveriesForCap st cfg overflow
        else (st, [])

    recoveryId = next_recovery_id stAfterEvictions
    recovery =
      RecoveryOrder
        { recovery_id = recoveryId,
          side = orderSide,
          price = orderPrice,
          volume = orderVolume,
          trade_id = orderTradeId,
          cycle = orderCycle,
          entry_price = orderEntryPrice,
          orphaned_at = now stAfterEvictions,
          entry_fee = orderEntryFee,
          entry_filled_at = orderEntryFilledAt,
          txid = orderTxid,
          reason = reason,
          regime_at_entry = orderRegimeAtEntry
        }
    stAfterMove =
      stAfterEvictions
        { orders = removeOrderByLocalId (orders stAfterEvictions) orderLocalId,
          recovery_orders = recovery_orders stAfterEvictions ++ [recovery],
          next_recovery_id = recoveryId + 1
        }
    orphanAction =
      OrphanOrderAction
        { local_id = orderLocalId,
          recovery_id = recoveryId,
          reason = reason
        }

    stAfterCycleAdvance =
      case orderTradeId of
        TradeA ->
          let stCooldown =
                if reentry_base_cooldown_sec cfg > 0
                  then
                    stAfterMove
                      { cooldown_until_a =
                          max
                            (cooldown_until_a stAfterMove)
                            (now stAfterMove + reentry_base_cooldown_sec cfg)
                      }
                  else stAfterMove
           in stCooldown {cycle_a = cycle_a stCooldown + 1}
        TradeB ->
          let stCooldown =
                if reentry_base_cooldown_sec cfg > 0
                  then
                    stAfterMove
                      { cooldown_until_b =
                          max
                            (cooldown_until_b stAfterMove)
                            (now stAfterMove + reentry_base_cooldown_sec cfg)
                      }
                  else stAfterMove
           in stCooldown {cycle_b = cycle_b stCooldown + 1}

    effectiveOrderSizeUsd = orderSizeForTrade orderTradeId defaultOrderSizeUsd maybeOrderSizes
    (stAfterFollowup, followupActions) =
      placeFollowupEntryAfterCycle
        stAfterCycleAdvance
        cfg
        orderTradeId
        effectiveOrderSizeUsd
        ( if orderTradeId == TradeA
            then T.pack "orphan_A"
            else T.pack "orphan_B"
        )

evictRecoveriesForCap :: PairState -> EngineConfig -> Int -> (PairState, [Action])
evictRecoveriesForCap st cfg overflow =
  (stFinal {recovery_orders = keptRecoveries}, actionsFinal)
  where
    market = market_price st
    rank RecoveryOrder {price = recPrice, orphaned_at = recOrphanedAt, recovery_id = recId} =
      (negate distance, recOrphanedAt, recId)
      where
        distance
          | market > 0 = abs (recPrice - market) / market
          | otherwise = 0.0

    ordered = sortOn rank (recovery_orders st)
    evictIds = map (\RecoveryOrder {recovery_id = rid} -> rid) (take overflow ordered)

    step (stAcc, actionsAcc, keptAcc) recovery@RecoveryOrder {recovery_id = recId}
      | recId `elem` evictIds =
          let RecoveryOrder
                { side = recSide,
                  price = recPrice,
                  volume = recVolume,
                  trade_id = recTradeId,
                  cycle = recCycle,
                  entry_price = recEntryPrice,
                  orphaned_at = recOrphanedAt,
                  entry_fee = recEntryFee,
                  entry_filled_at = recEntryFilledAt,
                  txid = recTxid,
                  regime_at_entry = recRegimeAtEntry
                } = recovery
              maybeCancelAction =
                if T.null recTxid
                  then []
                  else
                    [ ActCancelOrder
                        CancelOrderAction
                          { local_id = negate recId,
                            txid = recTxid,
                            reason = T.pack "recovery_cap_evict_priority"
                          }
                    ]
              fillPrice0 =
                if market_price stAcc > 0
                  then market_price stAcc
                  else recPrice
              fillPrice =
                if fillPrice0 <= 0
                  then
                    if recEntryPrice > 0
                      then recEntryPrice
                      else 0.0
                  else fillPrice0
              fillFee =
                max
                  0.0
                  (fillPrice * recVolume * (maker_fee_pct cfg / 100.0))
              pseudoOrder =
                OrderState
                  { local_id = -1,
                    side = recSide,
                    role = Exit,
                    price = recPrice,
                    volume = recVolume,
                    trade_id = recTradeId,
                    cycle = recCycle,
                    txid = T.empty,
                    placed_at = 0.0,
                    entry_price = recEntryPrice,
                    entry_fee = recEntryFee,
                    entry_filled_at =
                      if recEntryFilledAt > 0
                        then recEntryFilledAt
                        else recOrphanedAt,
                    regime_at_entry = recRegimeAtEntry
                  }
              (stAfterBook, cycleRecord, bookAction) =
                bookCycle stAcc pseudoOrder fillPrice fillFee (now stAcc) True
              CycleRecord {net_profit = cycleNetProfit} = cycleRecord
              stAfterLoss = updateLossCounters stAfterBook recTradeId cycleNetProfit cfg
              actionsWithBook = actionsAcc ++ maybeCancelAction ++ [ActBookCycle bookAction]
           in (stAfterLoss, actionsWithBook, keptAcc)
      | otherwise = (stAcc, actionsAcc, keptAcc ++ [recovery])

    (stFinal, actionsFinal, keptRecoveries) = foldl step (st, [], []) (recovery_orders st)

wouldOrphanS1Exit :: PairState -> PairPhase -> EngineConfig -> Bool
wouldOrphanS1Exit st phase cfg
  | phase /= S1a && phase /= S1b = False
  | otherwise = case firstExitOrder st of
      Nothing -> False
      Just OrderState {entry_filled_at = entryFilledAt, placed_at = placedAt, side = orderSide, price = orderPrice} ->
        age >= s1_orphan_after_sec cfg && movedAway
        where
          baseTs
            | entryFilledAt /= 0 = entryFilledAt
            | placedAt /= 0 = placedAt
            | otherwise = now st
          age = now st - baseTs
          movedAway =
            (orderSide == Sell && market_price st < orderPrice)
              || (orderSide == Buy && market_price st > orderPrice)

wouldOrphanS2Exit :: PairState -> EngineConfig -> Bool
wouldOrphanS2Exit st cfg = case s2_entered_at st of
  Nothing -> False
  Just enteredAt ->
    now st - enteredAt >= s2_orphan_after_sec cfg
      && hasBuyExit
      && hasSellExit
      && market_price st > 0
  where
    exitOrders = filter (\OrderState {role = orderRole} -> orderRole == Exit) (orders st)
    hasBuyExit = hasExitSide Buy exitOrders
    hasSellExit = hasExitSide Sell exitOrders

worseS2Exit :: PairState -> Maybe OrderState
worseS2Exit st = do
  let exitOrders = filter (\OrderState {role = orderRole} -> orderRole == Exit) (orders st)
  buyExit@OrderState {price = buyPrice} <- findExitBySide Buy exitOrders
  sellExit@OrderState {price = sellPrice} <- findExitBySide Sell exitOrders
  let market = market_price st
  if market <= 0
    then Nothing
    else
      let buyDist = abs (buyPrice - market) / market
          sellDist = abs (sellPrice - market) / market
       in Just (if buyDist > sellDist then buyExit else sellExit)

findExitBySide :: Side -> [OrderState] -> Maybe OrderState
findExitBySide sideToFind = find (\OrderState {side = orderSide} -> orderSide == sideToFind)

hasExitSide :: Side -> [OrderState] -> Bool
hasExitSide sideToMatch = any (\OrderState {side = orderSide} -> orderSide == sideToMatch)

firstExitOrder :: PairState -> Maybe OrderState
firstExitOrder st = find (\OrderState {role = orderRole} -> orderRole == Exit) (orders st)

handlePriceTick ::
  PairState ->
  Double ->
  Double ->
  EngineConfig ->
  Double ->
  Maybe [(TradeId, Double)] ->
  (PairState, [Action])
handlePriceTick st marketPrice eventTimestamp cfg defaultOrderSizeUsd maybeOrderSizes =
  refreshStaleEntries withTick cfg defaultOrderSizeUsd maybeOrderSizes
  where
    withTick =
      st
        { now = eventTimestamp,
          market_price = marketPrice,
          last_price_update_at = Just eventTimestamp
        }

refreshStaleEntries ::
  PairState ->
  EngineConfig ->
  Double ->
  Maybe [(TradeId, Double)] ->
  (PairState, [Action])
refreshStaleEntries st cfg defaultOrderSizeUsd maybeOrderSizes = go st (orders st)
  where
    go stCurrent [] = (stCurrent, [])
    go stCurrent (order : remaining)
      | orderRole /= Entry = go stCurrent remaining
      | drift <= refresh_pct cfg = go stCurrent remaining
      | now stCurrent < cooldownUntil = go stCurrent remaining
      | otherwise =
          if count >= max_consecutive_refreshes cfg
            then (stCapped, [])
            else (stWithCounters, cancelAction : followupActions)
      where
        OrderState
          { local_id = orderLocalId,
            side = orderSide,
            role = orderRole,
            price = orderPrice,
            trade_id = orderTradeId,
            cycle = orderCycle,
            txid = orderTxid
          } = order
        market = market_price stCurrent
        drift
          | market > 0 = abs (orderPrice - market) / market * 100.0
          | otherwise = 0.0
        isTradeA = orderTradeId == TradeA
        cooldownUntil =
          if isTradeA
            then refresh_cooldown_until_a stCurrent
            else refresh_cooldown_until_b stCurrent
        prevCountCheck =
          if isTradeA
            then consecutive_refreshes_a stCurrent
            else consecutive_refreshes_b stCurrent
        stAfterReset =
          if prevCountCheck >= max_consecutive_refreshes cfg && cooldownUntil > 0
            then
              if isTradeA
                then
                  stCurrent
                    { consecutive_refreshes_a = 0,
                      refresh_cooldown_until_a = 0.0
                    }
                else
                  stCurrent
                    { consecutive_refreshes_b = 0,
                      refresh_cooldown_until_b = 0.0
                    }
            else stCurrent
        direction =
          case orderSide of
            Buy ->
              if market_price stAfterReset < orderPrice
                then T.pack "down"
                else T.pack "up"
            Sell ->
              if market_price stAfterReset > orderPrice
                then T.pack "up"
                else T.pack "down"
        prevDirection =
          if isTradeA
            then last_refresh_direction_a stAfterReset
            else last_refresh_direction_b stAfterReset
        prevCount =
          if isTradeA
            then consecutive_refreshes_a stAfterReset
            else consecutive_refreshes_b stAfterReset
        count =
          if prevDirection == Just direction
            then prevCount + 1
            else 1
        stAfterCancel = stAfterReset {orders = removeOrderByLocalId (orders stAfterReset) orderLocalId}
        cancelAction =
          ActCancelOrder
            CancelOrderAction
              { local_id = orderLocalId,
                txid = orderTxid,
                reason = T.pack "stale_entry"
              }
        effectiveOrderSizeUsd = orderSizeForTrade orderTradeId defaultOrderSizeUsd maybeOrderSizes
        (stAfterAlloc, maybeNewOrder, maybePlaceAction) =
          newEntryOrder
            stAfterCancel
            cfg
            orderSide
            orderTradeId
            orderCycle
            effectiveOrderSizeUsd
            (T.pack "refresh_entry")
        (stAfterFollowup, followupActions) = attachMaybeEntry stAfterAlloc maybeNewOrder maybePlaceAction
        stWithCounters =
          if isTradeA
            then
              stAfterFollowup
                { consecutive_refreshes_a = count,
                  last_refresh_direction_a = Just direction
                }
            else
              stAfterFollowup
                { consecutive_refreshes_b = count,
                  last_refresh_direction_b = Just direction
                }
        stCapped =
          if isTradeA
            then
              stAfterReset
                { consecutive_refreshes_a = count,
                  last_refresh_direction_a = Just direction,
                  refresh_cooldown_until_a = now stAfterReset + refresh_cooldown_sec cfg
                }
            else
              stAfterReset
                { consecutive_refreshes_b = count,
                  last_refresh_direction_b = Just direction,
                  refresh_cooldown_until_b = now stAfterReset + refresh_cooldown_sec cfg
                }

handleFillEvent :: PairState -> FillEvent -> EngineConfig -> Double -> Maybe [(TradeId, Double)] -> (PairState, [Action])
handleFillEvent st FillEvent {order_local_id = orderLocalId, price = fillPrice, volume = fillVolume, fee = fillFee, timestamp = eventTimestamp} cfg defaultOrderSizeUsd maybeOrderSizes =
  case findOrderByLocalId stWithNow orderLocalId of
    Nothing -> (stWithNow, [])
    Just order@OrderState {role = orderRole}
      | orderRole == Entry -> handleEntryFill stWithNow order fillPrice fillVolume fillFee eventTimestamp cfg
      | otherwise -> handleExitFill stWithNow order fillPrice fillFee eventTimestamp cfg defaultOrderSizeUsd maybeOrderSizes
  where
    stWithNow = st {now = eventTimestamp}

handleEntryFill :: PairState -> OrderState -> Double -> Double -> Double -> Double -> EngineConfig -> (PairState, [Action])
handleEntryFill st order fillPrice fillVolume fillFee eventTimestamp cfg =
  (nextState, [ActPlaceOrder placeAction])
  where
    OrderState
      { local_id = orderLocalId,
        side = orderSide,
        trade_id = orderTradeId,
        cycle = orderCycle,
        regime_at_entry = orderRegimeAtEntry
      } = order
    stWithoutFilledOrder = st {orders = removeOrderByLocalId (orders st) orderLocalId}
    stWithEntryFee = stWithoutFilledOrder {total_fees = total_fees stWithoutFilledOrder + fillFee}
    exitSide = oppositeSide orderSide
    exitLocal = next_order_id stWithEntryFee
    exitOrderPrice = exitPrice fillPrice (market_price stWithEntryFee) exitSide cfg (effectiveProfitPct stWithEntryFee cfg)
    exitOrder =
      OrderState
        { local_id = exitLocal,
          side = exitSide,
          role = Exit,
          price = exitOrderPrice,
          volume = fillVolume,
          trade_id = orderTradeId,
          cycle = orderCycle,
          txid = T.empty,
          placed_at = eventTimestamp,
          entry_price = fillPrice,
          entry_fee = fillFee,
          entry_filled_at = eventTimestamp,
          regime_at_entry = orderRegimeAtEntry
        }
    withExitOrder =
      stWithEntryFee
        { orders = orders stWithEntryFee ++ [exitOrder],
          next_order_id = exitLocal + 1
        }
    nextState = clearS2FlagIfNotS2 withExitOrder
    placeAction =
      PlaceOrderAction
        { local_id = exitLocal,
          side = exitSide,
          role = Exit,
          price = exitOrderPrice,
          volume = fillVolume,
          trade_id = orderTradeId,
          cycle = orderCycle,
          post_only = True,
          reason = T.pack "entry_fill_exit"
        }

handleExitFill ::
  PairState ->
  OrderState ->
  Double ->
  Double ->
  Double ->
  EngineConfig ->
  Double ->
  Maybe [(TradeId, Double)] ->
  (PairState, [Action])
handleExitFill st order fillPrice fillFee eventTimestamp cfg defaultOrderSizeUsd maybeOrderSizes =
  (nextState, ActBookCycle bookAction : followActions)
  where
    OrderState {local_id = orderLocalId, trade_id = orderTradeId, cycle = orderCycle} = order
    stWithoutFilledOrder = st {orders = removeOrderByLocalId (orders st) orderLocalId}
    (stAfterBook, cycleRecord, bookAction) = bookCycle stWithoutFilledOrder order fillPrice fillFee eventTimestamp False
    CycleRecord {net_profit = cycleNetProfit} = cycleRecord
    stAfterLoss = updateLossCounters stAfterBook orderTradeId cycleNetProfit cfg
    stAfterCycle =
      case orderTradeId of
        TradeA -> stAfterLoss {cycle_a = max (cycle_a stAfterLoss) (orderCycle + 1)}
        TradeB -> stAfterLoss {cycle_b = max (cycle_b stAfterLoss) (orderCycle + 1)}
    effectiveOrderSizeUsd = orderSizeForTrade orderTradeId defaultOrderSizeUsd maybeOrderSizes
    (stAfterFollowup, followActions) =
      placeFollowupEntryAfterCycle
        stAfterCycle
        cfg
        orderTradeId
        effectiveOrderSizeUsd
        (T.pack "cycle_complete")
    nextState = clearS2FlagIfNotS2 stAfterFollowup

orderSizeForTrade :: TradeId -> Double -> Maybe [(TradeId, Double)] -> Double
orderSizeForTrade tradeId defaultOrderSize maybeOrderSizes =
  case maybeOrderSizes >>= lookup tradeId of
    Just value -> value
    Nothing -> defaultOrderSize

placeFollowupEntryAfterCycle :: PairState -> EngineConfig -> TradeId -> Double -> Text -> (PairState, [Action])
placeFollowupEntryAfterCycle st cfg tradeId orderSizeUsd reason =
  case tradeId of
    TradeA ->
      if long_only st || now st < cooldown_until_a st
        then (st, [])
        else
          let (stAfterAlloc, maybeOrder, maybeAction) =
                newEntryOrder st cfg Sell TradeA (cycle_a st) orderSizeUsd reason
           in attachMaybeEntry stAfterAlloc maybeOrder maybeAction
    TradeB ->
      if short_only st || now st < cooldown_until_b st
        then (st, [])
        else
          let (stAfterAlloc, maybeOrder, maybeAction) =
                newEntryOrder st cfg Buy TradeB (cycle_b st) orderSizeUsd reason
           in attachMaybeEntry stAfterAlloc maybeOrder maybeAction

attachMaybeEntry :: PairState -> Maybe OrderState -> Maybe PlaceOrderAction -> (PairState, [Action])
attachMaybeEntry st maybeOrder maybeAction =
  case (maybeOrder, maybeAction) of
    (Just order, Just action) ->
      ( st {orders = orders st ++ [order]},
        [ActPlaceOrder action]
      )
    _ -> (st, [])

newEntryOrder ::
  PairState ->
  EngineConfig ->
  Side ->
  TradeId ->
  Int ->
  Double ->
  Text ->
  (PairState, Maybe OrderState, Maybe PlaceOrderAction)
newEntryOrder st cfg sideForOrder tradeId cycleValue orderSizeUsd reason =
  case computeOrderVolume entryPrice cfg orderSizeUsd of
    Nothing -> (st, Nothing, Nothing)
    Just volumeValue ->
      ( st {next_order_id = localId + 1},
        Just order,
        Just action
      )
      where
        localId = next_order_id st
        order =
          OrderState
            { local_id = localId,
              side = sideForOrder,
              role = Entry,
              price = entryPrice,
              volume = volumeValue,
              trade_id = tradeId,
              cycle = cycleValue,
              txid = T.empty,
              placed_at = now st,
              entry_price = 0.0,
              entry_fee = 0.0,
              entry_filled_at = 0.0,
              regime_at_entry = Nothing
            }
        action =
          PlaceOrderAction
            { local_id = localId,
              side = sideForOrder,
              role = Entry,
              price = entryPrice,
              volume = volumeValue,
              trade_id = tradeId,
              cycle = cycleValue,
              post_only = True,
              reason = reason
            }
  where
    baseEntryPct = entryPctForTrade cfg tradeId
    (buyPrice, sellPrice) = entryPrices (market_price st) baseEntryPct cfg
    lossCount =
      case sideForOrder of
        Buy ->
          if tradeId == TradeB
            then consecutive_losses_b st
            else consecutive_losses_a st
        Sell ->
          if tradeId == TradeA
            then consecutive_losses_a st
            else consecutive_losses_b st
    backedOffPct = (baseEntryPct * entryBackoffMultiplier lossCount cfg) / 100.0
    proposedPrice =
      case sideForOrder of
        Buy -> roundPrice (market_price st * (1 - backedOffPct)) cfg
        Sell -> roundPrice (market_price st * (1 + backedOffPct)) cfg
    entryPrice
      | proposedPrice > 0 = proposedPrice
      | sideForOrder == Buy = buyPrice
      | otherwise = sellPrice

entryPctForTrade :: EngineConfig -> TradeId -> Double
entryPctForTrade cfg tradeId =
  if pct <= 0
    then entry_pct cfg
    else pct
  where
    pct =
      case tradeId of
        TradeA -> case entry_pct_a cfg of
          Just v | v > 0 -> v
          _ -> entry_pct cfg
        TradeB -> case entry_pct_b cfg of
          Just v | v > 0 -> v
          _ -> entry_pct cfg

entryPrices :: Double -> Double -> EngineConfig -> (Double, Double)
entryPrices marketPrice entryPct cfg = (buyPrice, sellPrice)
  where
    p = entryPct / 100.0
    buyPrice = roundPrice (marketPrice * (1 - p)) cfg
    sellPrice = roundPrice (marketPrice * (1 + p)) cfg

entryBackoffMultiplier :: Int -> EngineConfig -> Double
entryBackoffMultiplier lossCount cfg
  | lossCount < loss_backoff_start cfg = 1.0
  | otherwise =
      min
        (backoff_max_multiplier cfg)
        (1.0 + backoff_factor cfg * fromIntegral (lossCount - loss_backoff_start cfg + 1))

computeOrderVolume :: Double -> EngineConfig -> Double -> Maybe Double
computeOrderVolume priceValue cfg orderSizeUsd
  | priceValue <= 0 = Nothing
  | orderSizeUsd <= 0 = Nothing
  | minCost > 0 && orderSizeUsd < minCost = Nothing
  | volume < min_volume cfg = Nothing
  | minCost > 0 && volume * priceValue < minCost = Nothing
  | otherwise = Just volume
  where
    minCost = min_cost_usd cfg
    raw = orderSizeUsd / priceValue
    volume
      | volume_decimals cfg <= 0 = fromIntegral (round raw :: Integer)
      | otherwise = roundTo (volume_decimals cfg) raw

bookCycle ::
  PairState ->
  OrderState ->
  Double ->
  Double ->
  Double ->
  Bool ->
  (PairState, CycleRecord, BookCycleAction)
bookCycle st order fillPrice fillFee eventTimestamp fromRecovery =
  (nextState, cycleRecord, cycleAction)
  where
    OrderState
      { volume = orderVolume,
        trade_id = orderTradeId,
        entry_price = orderEntryPrice,
        entry_fee = orderEntryFee,
        entry_filled_at = orderEntryFilledAt,
        cycle = orderCycle,
        regime_at_entry = orderRegimeAtEntry
      } = order
    grossProfit =
      case orderTradeId of
        TradeA -> (orderEntryPrice - fillPrice) * orderVolume
        TradeB -> (fillPrice - orderEntryPrice) * orderVolume
    entryFee = orderEntryFee
    exitFee = fillFee
    feesPaid = entryFee + exitFee
    netProfit = grossProfit - feesPaid
    quoteFee =
      case orderTradeId of
        TradeA -> entryFee
        TradeB -> exitFee
    settledUsd = grossProfit - quoteFee
    cycleRecord =
      CycleRecord
        { trade_id = orderTradeId,
          cycle = orderCycle,
          entry_price = orderEntryPrice,
          exit_price = fillPrice,
          volume = orderVolume,
          gross_profit = grossProfit,
          fees = feesPaid,
          net_profit = netProfit,
          entry_fee = entryFee,
          exit_fee = exitFee,
          quote_fee = quoteFee,
          settled_usd = settledUsd,
          entry_time = orderEntryFilledAt,
          exit_time = eventTimestamp,
          from_recovery = fromRecovery,
          regime_at_entry = orderRegimeAtEntry
        }
    totalLoss =
      if netProfit < 0
        then today_realized_loss st + abs netProfit
        else today_realized_loss st
    nextState =
      st
        { total_profit = total_profit st + netProfit,
          total_settled_usd = total_settled_usd st + settledUsd,
          total_fees = total_fees st + fillFee,
          today_realized_loss = totalLoss,
          total_round_trips = total_round_trips st + 1,
          completed_cycles = completed_cycles st ++ [cycleRecord]
        }
    cycleAction =
      BookCycleAction
        { trade_id = orderTradeId,
          cycle = orderCycle,
          net_profit = netProfit,
          gross_profit = grossProfit,
          fees = feesPaid,
          settled_usd = settledUsd,
          from_recovery = fromRecovery
        }

updateLossCounters :: PairState -> TradeId -> Double -> EngineConfig -> PairState
updateLossCounters st tradeId netProfit cfg =
  case tradeId of
    TradeA ->
      st
        { consecutive_losses_a = lossesA',
          cooldown_until_a = cooldownA'
        }
      where
        cooldownA0 =
          if reentry_base_cooldown_sec cfg > 0
            then max (cooldown_until_a st) (now st + reentry_base_cooldown_sec cfg)
            else cooldown_until_a st
        lossesA' = if netProfit < 0 then consecutive_losses_a st + 1 else 0
        cooldownA' =
          if lossesA' >= loss_cooldown_start cfg
            then max cooldownA0 (now st + loss_cooldown_sec cfg)
            else cooldownA0
    TradeB ->
      st
        { consecutive_losses_b = lossesB',
          cooldown_until_b = cooldownB'
        }
      where
        cooldownB0 =
          if reentry_base_cooldown_sec cfg > 0
            then max (cooldown_until_b st) (now st + reentry_base_cooldown_sec cfg)
            else cooldown_until_b st
        lossesB' = if netProfit < 0 then consecutive_losses_b st + 1 else 0
        cooldownB' =
          if lossesB' >= loss_cooldown_start cfg
            then max cooldownB0 (now st + loss_cooldown_sec cfg)
            else cooldownB0

handleRecoveryFillEvent :: PairState -> RecoveryFillEvent -> EngineConfig -> (PairState, [Action])
handleRecoveryFillEvent st RecoveryFillEvent {recovery_id = recoveryId, price = fillPrice, fee = fillFee, timestamp = eventTimestamp} cfg =
  case findRecoveryById stWithNow recoveryId of
    Nothing -> (stWithNow, [])
    Just recovery -> (nextState, [ActBookCycle bookAction])
      where
        RecoveryOrder
          { recovery_id = recId,
            side = recSide,
            price = recPrice,
            volume = recVolume,
            trade_id = recTradeId,
            cycle = recCycle,
            entry_price = recEntryPrice,
            orphaned_at = recOrphanedAt,
            entry_fee = recEntryFee,
            entry_filled_at = recEntryFilledAt,
            regime_at_entry = recRegimeAtEntry
          } = recovery
        stWithoutRecovery = stWithNow {recovery_orders = removeRecoveryById (recovery_orders stWithNow) recId}
        pseudoOrder =
          OrderState
            { local_id = -1,
              side = recSide,
              role = Exit,
              price = recPrice,
              volume = recVolume,
              trade_id = recTradeId,
              cycle = recCycle,
              txid = T.empty,
              placed_at = 0.0,
              entry_price = recEntryPrice,
              entry_fee = recEntryFee,
              entry_filled_at =
                if recEntryFilledAt > 0
                  then recEntryFilledAt
                  else recOrphanedAt,
              regime_at_entry = recRegimeAtEntry
            }
        (stAfterBook, cycleRecord, bookAction) = bookCycle stWithoutRecovery pseudoOrder fillPrice fillFee eventTimestamp True
        CycleRecord {net_profit = cycleNetProfit} = cycleRecord
        stAfterLoss = updateLossCounters stAfterBook recTradeId cycleNetProfit cfg
        nextState = clearS2FlagIfNotS2 stAfterLoss
  where
    stWithNow = st {now = eventTimestamp}

findRecoveryById :: PairState -> Int -> Maybe RecoveryOrder
findRecoveryById st recoveryId =
  find (\RecoveryOrder {recovery_id = currentRecoveryId} -> currentRecoveryId == recoveryId) (recovery_orders st)

removeRecoveryById :: [RecoveryOrder] -> Int -> [RecoveryOrder]
removeRecoveryById recoveryOrdersInState recoveryId =
  filter (\RecoveryOrder {recovery_id = currentRecoveryId} -> currentRecoveryId /= recoveryId) recoveryOrdersInState

findOrderByLocalId :: PairState -> Int -> Maybe OrderState
findOrderByLocalId st orderLocalId =
  find (\OrderState {local_id = currentOrderId} -> currentOrderId == orderLocalId) (orders st)

removeOrderByLocalId :: [OrderState] -> Int -> [OrderState]
removeOrderByLocalId ordersInState orderLocalId =
  filter (\OrderState {local_id = currentOrderId} -> currentOrderId /= orderLocalId) ordersInState

oppositeSide :: Side -> Side
oppositeSide Buy = Sell
oppositeSide Sell = Buy

effectiveProfitPct :: PairState -> EngineConfig -> Double
effectiveProfitPct st cfg
  | profit_pct_runtime st /= 0 = profit_pct_runtime st
  | otherwise = profit_pct cfg

exitPrice :: Double -> Double -> Side -> EngineConfig -> Double -> Double
exitPrice entryFill marketPrice side cfg profitPctValue
  | side == Sell = roundPrice (max (entryFill * (1 + p)) (marketPrice * (1 + e))) cfg
  | otherwise = roundPrice (min (entryFill * (1 - p)) (marketPrice * (1 - e))) cfg
  where
    p = profitPctValue / 100.0
    e = entry_pct cfg / 100.0

roundPrice :: Double -> EngineConfig -> Double
roundPrice value cfg
  | price_decimals cfg <= 0 = fromIntegral (round value :: Integer)
  | otherwise =
      let factor = 10 ^^ price_decimals cfg :: Double
       in fromIntegral (round (value * factor) :: Integer) / factor

roundTo :: Int -> Double -> Double
roundTo decimals value
  | decimals <= 0 = fromIntegral (round value :: Integer)
  | otherwise =
      let factor = 10 ^^ decimals :: Double
       in fromIntegral (round (value * factor) :: Integer) / factor

clearS2FlagIfNotS2 :: PairState -> PairState
clearS2FlagIfNotS2 st = case s2_entered_at st of
  Nothing -> st
  Just _ ->
    if derivePhase st == S2
      then st
      else st {s2_entered_at = Nothing}
