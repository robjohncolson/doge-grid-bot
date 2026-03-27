{-# LANGUAGE DisambiguateRecordFields #-}
{-# LANGUAGE OverloadedStrings #-}

module DogeCore.Invariants
  ( derivePhase,
    checkInvariants,
  )
where

import Data.List qualified as L
import Data.Text (Text)
import DogeCore.Types

derivePhase :: PairState -> PairPhase
derivePhase st
  | hasBuyExit && hasSellExit = S2
  | hasBuyExit = S1a
  | hasSellExit = S1b
  | otherwise = S0
  where
    hasBuyExit = any isBuyExit (orders st)
    hasSellExit = any isSellExit (orders st)
    isBuyExit OrderState {side = orderSide, role = orderRole} =
      orderSide == Buy && orderRole == Exit
    isSellExit OrderState {side = orderSide, role = orderRole} =
      orderSide == Sell && orderRole == Exit

checkInvariants :: PairState -> [Text]
checkInvariants st =
  duplicateIdViolation
    <> phaseViolations
    <> s2FlagViolations
    <> orderViolations
    <> cycleViolations
  where
    phase = derivePhase st
    allOrders = orders st
    entries = filter isEntry allOrders
    exits = filter isExit allOrders
    buyEntries = filter isBuy entries
    sellEntries = filter isSell entries
    buyExits = filter isBuy exits
    sellExits = filter isSell exits

    isEntry OrderState {role = orderRole} = orderRole == Entry
    isExit OrderState {role = orderRole} = orderRole == Exit
    isBuy OrderState {side = orderSide} = orderSide == Buy
    isSell OrderState {side = orderSide} = orderSide == Sell
    orderLocalId OrderState {local_id = orderId} = orderId

    duplicateIdViolation =
      let ids = map orderLocalId allOrders
       in if length ids /= length (L.nub ids) then ["duplicate order local_id"] else []

    phaseViolations = case phase of
      S0 ->
        if long_only st
          then
            [ "S0 long_only must be exactly one buy entry"
              | length buyEntries /= 1 || not (null sellEntries) || not (null exits)
            ]
          else
            if short_only st
              then
                [ "S0 short_only must be exactly one sell entry"
                  | length sellEntries /= 1 || not (null buyEntries) || not (null exits)
                ]
              else
                [ "S0 must be exactly A sell entry + B buy entry"
                  | length buyEntries /= 1 || length sellEntries /= 1 || not (null exits)
                ]
      S1a ->
        if short_only st
          then
            [ "S1a short_only must have one buy exit"
              | length buyExits /= 1
            ]
          else
            [ "S1a must be one buy exit + one buy entry"
              | length buyExits /= 1
                  || length buyEntries /= 1
                  || not (null sellEntries)
                  || not (null sellExits)
            ]
      S1b ->
        if long_only st
          then
            [ "S1b long_only must have one sell exit"
              | length sellExits /= 1
            ]
          else
            [ "S1b must be one sell exit + one sell entry"
              | length sellExits /= 1
                  || length sellEntries /= 1
                  || not (null buyEntries)
                  || not (null buyExits)
            ]
      S2 ->
        [ "S2 must be one buy exit + one sell exit only"
          | length buyExits /= 1 || length sellExits /= 1 || not (null entries)
        ]

    s2FlagViolations =
      [ "s2_entered_at must be null outside S2"
        | phase /= S2 && s2_entered_at st /= Nothing
      ]

    orderViolations = concatMap perOrder allOrders
    perOrder OrderState {cycle = orderCycle, role = orderRole, entry_price = orderEntryPrice, volume = orderVolume} =
      [ "order cycle must be >= 1"
        | orderCycle < 1
      ]
        <> [ "exit must carry entry_price"
             | orderRole == Exit && orderEntryPrice <= 0
           ]
        <> [ "order volume must be > 0"
             | orderVolume <= 0
           ]

    cycleViolations =
      [ "cycle counters must be >= 1"
        | cycle_a st < 1 || cycle_b st < 1
      ]
