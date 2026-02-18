{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE DuplicateRecordFields #-}
{-# LANGUAGE OverloadedStrings #-}

module DogeCore.Types
  ( PairPhase (..),
    Side (..),
    Role (..),
    TradeId (..),
    EngineConfig (..),
    OrderState (..),
    RecoveryOrder (..),
    CycleRecord (..),
    PairState (..),
    PriceTick (..),
    TimerTick (..),
    FillEvent (..),
    RecoveryFillEvent (..),
    RecoveryCancelEvent (..),
    Event (..),
    PlaceOrderAction (..),
    CancelOrderAction (..),
    OrphanOrderAction (..),
    BookCycleAction (..),
    Action (..),
    normalizeRegimeId,
  )
where

import Data.Aeson
import Data.Aeson.KeyMap qualified as KM
import Data.Aeson.Types (Parser)
import Data.Char (isDigit)
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Read qualified as TR
import GHC.Generics (Generic)

data PairPhase = S0 | S1a | S1b | S2
  deriving (Eq, Show)

instance ToJSON PairPhase where
  toJSON S0 = String "S0"
  toJSON S1a = String "S1a"
  toJSON S1b = String "S1b"
  toJSON S2 = String "S2"

instance FromJSON PairPhase where
  parseJSON = withText "PairPhase" $ \t -> case t of
    "S0" -> pure S0
    "S1a" -> pure S1a
    "S1b" -> pure S1b
    "S2" -> pure S2
    _ -> fail ("Unknown PairPhase: " <> T.unpack t)

data Side = Buy | Sell
  deriving (Eq, Show)

instance ToJSON Side where
  toJSON Buy = String "buy"
  toJSON Sell = String "sell"

instance FromJSON Side where
  parseJSON = withText "Side" $ \t -> case T.toLower t of
    "buy" -> pure Buy
    "sell" -> pure Sell
    _ -> fail ("Unknown Side: " <> T.unpack t)

data Role = Entry | Exit
  deriving (Eq, Show)

instance ToJSON Role where
  toJSON Entry = String "entry"
  toJSON Exit = String "exit"

instance FromJSON Role where
  parseJSON = withText "Role" $ \t -> case T.toLower t of
    "entry" -> pure Entry
    "exit" -> pure Exit
    _ -> fail ("Unknown Role: " <> T.unpack t)

data TradeId = TradeA | TradeB
  deriving (Eq, Show)

instance ToJSON TradeId where
  toJSON TradeA = String "A"
  toJSON TradeB = String "B"

instance FromJSON TradeId where
  parseJSON = withText "TradeId" $ \t -> case T.toUpper t of
    "A" -> pure TradeA
    "B" -> pure TradeB
    _ -> fail ("Unknown TradeId: " <> T.unpack t)

data EngineConfig = EngineConfig
  { entry_pct :: Double,
    entry_pct_a :: Maybe Double,
    entry_pct_b :: Maybe Double,
    profit_pct :: Double,
    refresh_pct :: Double,
    order_size_usd :: Double,
    price_decimals :: Int,
    volume_decimals :: Int,
    min_volume :: Double,
    min_cost_usd :: Double,
    maker_fee_pct :: Double,
    stale_price_max_age_sec :: Double,
    s1_orphan_after_sec :: Double,
    s2_orphan_after_sec :: Double,
    loss_backoff_start :: Int,
    loss_cooldown_start :: Int,
    loss_cooldown_sec :: Double,
    reentry_base_cooldown_sec :: Double,
    backoff_factor :: Double,
    backoff_max_multiplier :: Double,
    max_consecutive_refreshes :: Int,
    refresh_cooldown_sec :: Double,
    max_recovery_slots :: Int,
    sticky_mode_enabled :: Bool
  }
  deriving (Eq, Show, Generic)

instance ToJSON EngineConfig where
  toJSON = genericToJSON defaultOptions

defaultEngineConfig :: EngineConfig
defaultEngineConfig =
  EngineConfig
    { entry_pct = 0.2,
      entry_pct_a = Nothing,
      entry_pct_b = Nothing,
      profit_pct = 1.0,
      refresh_pct = 1.0,
      order_size_usd = 2.0,
      price_decimals = 6,
      volume_decimals = 0,
      min_volume = 13.0,
      min_cost_usd = 0.0,
      maker_fee_pct = 0.25,
      stale_price_max_age_sec = 60.0,
      s1_orphan_after_sec = 1350.0,
      s2_orphan_after_sec = 1800.0,
      loss_backoff_start = 3,
      loss_cooldown_start = 5,
      loss_cooldown_sec = 900.0,
      reentry_base_cooldown_sec = 0.0,
      backoff_factor = 0.5,
      backoff_max_multiplier = 5.0,
      max_consecutive_refreshes = 3,
      refresh_cooldown_sec = 300.0,
      max_recovery_slots = 2,
      sticky_mode_enabled = False
    }

instance FromJSON EngineConfig where
  parseJSON = withObject "EngineConfig" $ \o -> do
    entry_pct <- withDefault o "entry_pct" (entry_pct defaultEngineConfig)
    entry_pct_a <- o .:? "entry_pct_a"
    entry_pct_b <- o .:? "entry_pct_b"
    profit_pct <- withDefault o "profit_pct" (profit_pct defaultEngineConfig)
    refresh_pct <- withDefault o "refresh_pct" (refresh_pct defaultEngineConfig)
    order_size_usd <- withDefault o "order_size_usd" (order_size_usd defaultEngineConfig)
    price_decimals <- withDefault o "price_decimals" (price_decimals defaultEngineConfig)
    volume_decimals <- withDefault o "volume_decimals" (volume_decimals defaultEngineConfig)
    min_volume <- withDefault o "min_volume" (min_volume defaultEngineConfig)
    min_cost_usd <- withDefault o "min_cost_usd" (min_cost_usd defaultEngineConfig)
    maker_fee_pct <- withDefault o "maker_fee_pct" (maker_fee_pct defaultEngineConfig)
    stale_price_max_age_sec <- withDefault o "stale_price_max_age_sec" (stale_price_max_age_sec defaultEngineConfig)
    s1_orphan_after_sec <- withDefault o "s1_orphan_after_sec" (s1_orphan_after_sec defaultEngineConfig)
    s2_orphan_after_sec <- withDefault o "s2_orphan_after_sec" (s2_orphan_after_sec defaultEngineConfig)
    loss_backoff_start <- withDefault o "loss_backoff_start" (loss_backoff_start defaultEngineConfig)
    loss_cooldown_start <- withDefault o "loss_cooldown_start" (loss_cooldown_start defaultEngineConfig)
    loss_cooldown_sec <- withDefault o "loss_cooldown_sec" (loss_cooldown_sec defaultEngineConfig)
    reentry_base_cooldown_sec <- withDefault o "reentry_base_cooldown_sec" (reentry_base_cooldown_sec defaultEngineConfig)
    backoff_factor <- withDefault o "backoff_factor" (backoff_factor defaultEngineConfig)
    backoff_max_multiplier <- withDefault o "backoff_max_multiplier" (backoff_max_multiplier defaultEngineConfig)
    max_consecutive_refreshes <- withDefault o "max_consecutive_refreshes" (max_consecutive_refreshes defaultEngineConfig)
    refresh_cooldown_sec <- withDefault o "refresh_cooldown_sec" (refresh_cooldown_sec defaultEngineConfig)
    max_recovery_slots <- withDefault o "max_recovery_slots" (max_recovery_slots defaultEngineConfig)
    sticky_mode_enabled <- withDefault o "sticky_mode_enabled" (sticky_mode_enabled defaultEngineConfig)
    pure
      EngineConfig
        { entry_pct = entry_pct,
          entry_pct_a = entry_pct_a,
          entry_pct_b = entry_pct_b,
          profit_pct = profit_pct,
          refresh_pct = refresh_pct,
          order_size_usd = order_size_usd,
          price_decimals = price_decimals,
          volume_decimals = volume_decimals,
          min_volume = min_volume,
          min_cost_usd = min_cost_usd,
          maker_fee_pct = maker_fee_pct,
          stale_price_max_age_sec = stale_price_max_age_sec,
          s1_orphan_after_sec = s1_orphan_after_sec,
          s2_orphan_after_sec = s2_orphan_after_sec,
          loss_backoff_start = loss_backoff_start,
          loss_cooldown_start = loss_cooldown_start,
          loss_cooldown_sec = loss_cooldown_sec,
          reentry_base_cooldown_sec = reentry_base_cooldown_sec,
          backoff_factor = backoff_factor,
          backoff_max_multiplier = backoff_max_multiplier,
          max_consecutive_refreshes = max_consecutive_refreshes,
          refresh_cooldown_sec = refresh_cooldown_sec,
          max_recovery_slots = max_recovery_slots,
          sticky_mode_enabled = sticky_mode_enabled
        }

data OrderState = OrderState
  { local_id :: Int,
    side :: Side,
    role :: Role,
    price :: Double,
    volume :: Double,
    trade_id :: TradeId,
    cycle :: Int,
    txid :: Text,
    placed_at :: Double,
    entry_price :: Double,
    entry_fee :: Double,
    entry_filled_at :: Double,
    regime_at_entry :: Maybe Int
  }
  deriving (Eq, Show, Generic)

instance ToJSON OrderState where
  toJSON = genericToJSON defaultOptions

instance FromJSON OrderState where
  parseJSON = withObject "OrderState" $ \o -> do
    local_id <- withRequired o "local_id"
    side <- withRequired o "side"
    role <- withRequired o "role"
    price <- withRequired o "price"
    volume <- withRequired o "volume"
    trade_id <- withRequired o "trade_id"
    cycle <- withRequired o "cycle"
    txid <- withDefault o "txid" ""
    placed_at <- withDefault o "placed_at" 0.0
    entry_price <- withDefault o "entry_price" 0.0
    entry_fee <- withDefault o "entry_fee" 0.0
    entry_filled_at <- withDefault o "entry_filled_at" 0.0
    regimeRaw <- withDefault o "regime_at_entry" Null
    let regime_at_entry = normalizeRegimeId regimeRaw
    pure
      OrderState
        { local_id = local_id,
          side = side,
          role = role,
          price = price,
          volume = volume,
          trade_id = trade_id,
          cycle = cycle,
          txid = txid,
          placed_at = placed_at,
          entry_price = entry_price,
          entry_fee = entry_fee,
          entry_filled_at = entry_filled_at,
          regime_at_entry = regime_at_entry
        }

data RecoveryOrder = RecoveryOrder
  { recovery_id :: Int,
    side :: Side,
    price :: Double,
    volume :: Double,
    trade_id :: TradeId,
    cycle :: Int,
    entry_price :: Double,
    orphaned_at :: Double,
    entry_fee :: Double,
    entry_filled_at :: Double,
    txid :: Text,
    reason :: Text,
    regime_at_entry :: Maybe Int
  }
  deriving (Eq, Show, Generic)

instance ToJSON RecoveryOrder where
  toJSON = genericToJSON defaultOptions

instance FromJSON RecoveryOrder where
  parseJSON = withObject "RecoveryOrder" $ \o -> do
    recovery_id <- withRequired o "recovery_id"
    side <- withRequired o "side"
    price <- withRequired o "price"
    volume <- withRequired o "volume"
    trade_id <- withRequired o "trade_id"
    cycle <- withRequired o "cycle"
    entry_price <- withRequired o "entry_price"
    orphaned_at <- withRequired o "orphaned_at"
    entry_fee <- withDefault o "entry_fee" 0.0
    entry_filled_at <- withDefault o "entry_filled_at" 0.0
    txid <- withDefault o "txid" ""
    reason <- withDefault o "reason" "stale"
    regimeRaw <- withDefault o "regime_at_entry" Null
    let regime_at_entry = normalizeRegimeId regimeRaw
    pure
      RecoveryOrder
        { recovery_id = recovery_id,
          side = side,
          price = price,
          volume = volume,
          trade_id = trade_id,
          cycle = cycle,
          entry_price = entry_price,
          orphaned_at = orphaned_at,
          entry_fee = entry_fee,
          entry_filled_at = entry_filled_at,
          txid = txid,
          reason = reason,
          regime_at_entry = regime_at_entry
        }

data CycleRecord = CycleRecord
  { trade_id :: TradeId,
    cycle :: Int,
    entry_price :: Double,
    exit_price :: Double,
    volume :: Double,
    gross_profit :: Double,
    fees :: Double,
    net_profit :: Double,
    entry_fee :: Double,
    exit_fee :: Double,
    quote_fee :: Double,
    settled_usd :: Double,
    entry_time :: Double,
    exit_time :: Double,
    from_recovery :: Bool,
    regime_at_entry :: Maybe Int
  }
  deriving (Eq, Show, Generic)

instance ToJSON CycleRecord where
  toJSON = genericToJSON defaultOptions

instance FromJSON CycleRecord where
  parseJSON = withObject "CycleRecord" $ \o -> do
    trade_id <- withRequired o "trade_id"
    cycle <- withRequired o "cycle"
    entry_price <- withRequired o "entry_price"
    exit_price <- withRequired o "exit_price"
    volume <- withRequired o "volume"
    gross_profit <- withRequired o "gross_profit"
    fees <- withRequired o "fees"
    net_profit <- withRequired o "net_profit"
    entry_fee <- withDefault o "entry_fee" 0.0
    exit_fee <- withDefault o "exit_fee" 0.0
    quote_fee <- withDefault o "quote_fee" 0.0
    settled_usd <- withDefault o "settled_usd" 0.0
    entry_time <- withDefault o "entry_time" 0.0
    exit_time <- withDefault o "exit_time" 0.0
    from_recovery <- withDefault o "from_recovery" False
    regimeRaw <- withDefault o "regime_at_entry" Null
    let regime_at_entry = normalizeRegimeId regimeRaw
    pure
      CycleRecord
        { trade_id = trade_id,
          cycle = cycle,
          entry_price = entry_price,
          exit_price = exit_price,
          volume = volume,
          gross_profit = gross_profit,
          fees = fees,
          net_profit = net_profit,
          entry_fee = entry_fee,
          exit_fee = exit_fee,
          quote_fee = quote_fee,
          settled_usd = settled_usd,
          entry_time = entry_time,
          exit_time = exit_time,
          from_recovery = from_recovery,
          regime_at_entry = regime_at_entry
        }

data PairState = PairState
  { market_price :: Double,
    now :: Double,
    orders :: [OrderState],
    recovery_orders :: [RecoveryOrder],
    completed_cycles :: [CycleRecord],
    cycle_a :: Int,
    cycle_b :: Int,
    next_order_id :: Int,
    next_recovery_id :: Int,
    total_profit :: Double,
    total_settled_usd :: Double,
    total_fees :: Double,
    today_realized_loss :: Double,
    total_round_trips :: Int,
    s2_entered_at :: Maybe Double,
    last_price_update_at :: Maybe Double,
    consecutive_losses_a :: Int,
    consecutive_losses_b :: Int,
    cooldown_until_a :: Double,
    cooldown_until_b :: Double,
    long_only :: Bool,
    short_only :: Bool,
    mode_source :: Text,
    consecutive_refreshes_a :: Int,
    consecutive_refreshes_b :: Int,
    last_refresh_direction_a :: Maybe Text,
    last_refresh_direction_b :: Maybe Text,
    refresh_cooldown_until_a :: Double,
    refresh_cooldown_until_b :: Double,
    profit_pct_runtime :: Double
  }
  deriving (Eq, Show, Generic)

instance ToJSON PairState where
  toJSON = genericToJSON defaultOptions

instance FromJSON PairState where
  parseJSON = withObject "PairState" $ \o -> do
    modeRaw <- withDefault o "mode_source" "none"
    let modeNorm = T.toLower modeRaw
    let modeVal
          | modeNorm `elem` ["none", "balance", "regime"] = modeNorm
          | otherwise = "none"
    market_price <- withDefault o "market_price" 0.0
    now <- withDefault o "now" 0.0
    orders <- withDefault o "orders" []
    recovery_orders <- withDefault o "recovery_orders" []
    completed_cycles <- withDefault o "completed_cycles" []
    cycle_a <- withDefault o "cycle_a" 1
    cycle_b <- withDefault o "cycle_b" 1
    next_order_id <- withDefault o "next_order_id" 1
    next_recovery_id <- withDefault o "next_recovery_id" 1
    total_profit <- withDefault o "total_profit" 0.0
    total_settled_usd <- withDefault o "total_settled_usd" total_profit
    total_fees <- withDefault o "total_fees" 0.0
    today_realized_loss <- withDefault o "today_realized_loss" 0.0
    total_round_trips <- withDefault o "total_round_trips" 0
    s2_entered_at <- o .:? "s2_entered_at"
    last_price_update_at <- o .:? "last_price_update_at"
    consecutive_losses_a <- withDefault o "consecutive_losses_a" 0
    consecutive_losses_b <- withDefault o "consecutive_losses_b" 0
    cooldown_until_a <- withDefault o "cooldown_until_a" 0.0
    cooldown_until_b <- withDefault o "cooldown_until_b" 0.0
    long_only <- withDefault o "long_only" False
    short_only <- withDefault o "short_only" False
    consecutive_refreshes_a <- withDefault o "consecutive_refreshes_a" 0
    consecutive_refreshes_b <- withDefault o "consecutive_refreshes_b" 0
    last_refresh_direction_a <- o .:? "last_refresh_direction_a"
    last_refresh_direction_b <- o .:? "last_refresh_direction_b"
    refresh_cooldown_until_a <- withDefault o "refresh_cooldown_until_a" 0.0
    refresh_cooldown_until_b <- withDefault o "refresh_cooldown_until_b" 0.0
    profit_pct_runtime <- withDefault o "profit_pct_runtime" 1.0
    pure
      PairState
        { market_price = market_price,
          now = now,
          orders = orders,
          recovery_orders = recovery_orders,
          completed_cycles = completed_cycles,
          cycle_a = cycle_a,
          cycle_b = cycle_b,
          next_order_id = next_order_id,
          next_recovery_id = next_recovery_id,
          total_profit = total_profit,
          total_settled_usd = total_settled_usd,
          total_fees = total_fees,
          today_realized_loss = today_realized_loss,
          total_round_trips = total_round_trips,
          s2_entered_at = s2_entered_at,
          last_price_update_at = last_price_update_at,
          consecutive_losses_a = consecutive_losses_a,
          consecutive_losses_b = consecutive_losses_b,
          cooldown_until_a = cooldown_until_a,
          cooldown_until_b = cooldown_until_b,
          long_only = long_only,
          short_only = short_only,
          mode_source = modeVal,
          consecutive_refreshes_a = consecutive_refreshes_a,
          consecutive_refreshes_b = consecutive_refreshes_b,
          last_refresh_direction_a = last_refresh_direction_a,
          last_refresh_direction_b = last_refresh_direction_b,
          refresh_cooldown_until_a = refresh_cooldown_until_a,
          refresh_cooldown_until_b = refresh_cooldown_until_b,
          profit_pct_runtime = profit_pct_runtime
        }

data PriceTick = PriceTick
  { price :: Double,
    timestamp :: Double
  }
  deriving (Eq, Show, Generic)

instance ToJSON PriceTick where
  toJSON = genericToJSON defaultOptions

instance FromJSON PriceTick where
  parseJSON = genericParseJSON defaultOptions

data TimerTick = TimerTick
  { timestamp :: Double
  }
  deriving (Eq, Show, Generic)

instance ToJSON TimerTick where
  toJSON = genericToJSON defaultOptions

instance FromJSON TimerTick where
  parseJSON = genericParseJSON defaultOptions

data FillEvent = FillEvent
  { order_local_id :: Int,
    txid :: Text,
    side :: Side,
    price :: Double,
    volume :: Double,
    fee :: Double,
    timestamp :: Double
  }
  deriving (Eq, Show, Generic)

instance ToJSON FillEvent where
  toJSON = genericToJSON defaultOptions

instance FromJSON FillEvent where
  parseJSON = genericParseJSON defaultOptions

data RecoveryFillEvent = RecoveryFillEvent
  { recovery_id :: Int,
    txid :: Text,
    side :: Side,
    price :: Double,
    volume :: Double,
    fee :: Double,
    timestamp :: Double
  }
  deriving (Eq, Show, Generic)

instance ToJSON RecoveryFillEvent where
  toJSON = genericToJSON defaultOptions

instance FromJSON RecoveryFillEvent where
  parseJSON = genericParseJSON defaultOptions

data RecoveryCancelEvent = RecoveryCancelEvent
  { recovery_id :: Int,
    txid :: Text,
    timestamp :: Double
  }
  deriving (Eq, Show, Generic)

instance ToJSON RecoveryCancelEvent where
  toJSON = genericToJSON defaultOptions

instance FromJSON RecoveryCancelEvent where
  parseJSON = genericParseJSON defaultOptions

data Event
  = EvPriceTick PriceTick
  | EvTimerTick TimerTick
  | EvFillEvent FillEvent
  | EvRecoveryFillEvent RecoveryFillEvent
  | EvRecoveryCancelEvent RecoveryCancelEvent
  deriving (Eq, Show)

instance ToJSON Event where
  toJSON (EvPriceTick x) = toJSON x
  toJSON (EvTimerTick x) = toJSON x
  toJSON (EvFillEvent x) = toJSON x
  toJSON (EvRecoveryFillEvent x) = toJSON x
  toJSON (EvRecoveryCancelEvent x) = toJSON x

instance FromJSON Event where
  parseJSON = withObject "Event" $ \o -> do
    let has k = KM.member k o
    if has "order_local_id" && has "txid" && has "side" && has "price" && has "volume" && has "fee" && has "timestamp"
      then EvFillEvent <$> parseJSON (Object o)
      else
        if has "recovery_id" && has "txid" && has "side" && has "price" && has "volume" && has "fee" && has "timestamp"
          then EvRecoveryFillEvent <$> parseJSON (Object o)
          else
            if has "recovery_id" && has "txid" && has "timestamp"
              then EvRecoveryCancelEvent <$> parseJSON (Object o)
              else
                if has "timestamp" && has "price"
                  then EvPriceTick <$> parseJSON (Object o)
                  else
                    if has "timestamp"
                      then EvTimerTick <$> parseJSON (Object o)
                      else fail "Unable to decode Event from payload fields"

data PlaceOrderAction = PlaceOrderAction
  { local_id :: Int,
    side :: Side,
    role :: Role,
    price :: Double,
    volume :: Double,
    trade_id :: TradeId,
    cycle :: Int,
    post_only :: Bool,
    reason :: Text
  }
  deriving (Eq, Show, Generic)

instance ToJSON PlaceOrderAction where
  toJSON = genericToJSON defaultOptions

instance FromJSON PlaceOrderAction where
  parseJSON = withObject "PlaceOrderAction" $ \o -> do
    local_id <- withRequired o "local_id"
    side <- withRequired o "side"
    role <- withRequired o "role"
    price <- withRequired o "price"
    volume <- withRequired o "volume"
    trade_id <- withRequired o "trade_id"
    cycle <- withRequired o "cycle"
    post_only <- withDefault o "post_only" True
    reason <- withDefault o "reason" ""
    pure
      PlaceOrderAction
        { local_id = local_id,
          side = side,
          role = role,
          price = price,
          volume = volume,
          trade_id = trade_id,
          cycle = cycle,
          post_only = post_only,
          reason = reason
        }

data CancelOrderAction = CancelOrderAction
  { local_id :: Int,
    txid :: Text,
    reason :: Text
  }
  deriving (Eq, Show, Generic)

instance ToJSON CancelOrderAction where
  toJSON = genericToJSON defaultOptions

instance FromJSON CancelOrderAction where
  parseJSON = withObject "CancelOrderAction" $ \o -> do
    local_id <- withRequired o "local_id"
    txid <- withRequired o "txid"
    reason <- withDefault o "reason" ""
    pure
      CancelOrderAction
        { local_id = local_id,
          txid = txid,
          reason = reason
        }

data OrphanOrderAction = OrphanOrderAction
  { local_id :: Int,
    recovery_id :: Int,
    reason :: Text
  }
  deriving (Eq, Show, Generic)

instance ToJSON OrphanOrderAction where
  toJSON = genericToJSON defaultOptions

instance FromJSON OrphanOrderAction where
  parseJSON = withObject "OrphanOrderAction" $ \o -> do
    local_id <- withRequired o "local_id"
    recovery_id <- withRequired o "recovery_id"
    reason <- withDefault o "reason" ""
    pure
      OrphanOrderAction
        { local_id = local_id,
          recovery_id = recovery_id,
          reason = reason
        }

data BookCycleAction = BookCycleAction
  { trade_id :: TradeId,
    cycle :: Int,
    net_profit :: Double,
    gross_profit :: Double,
    fees :: Double,
    settled_usd :: Double,
    from_recovery :: Bool
  }
  deriving (Eq, Show, Generic)

instance ToJSON BookCycleAction where
  toJSON = genericToJSON defaultOptions

instance FromJSON BookCycleAction where
  parseJSON = withObject "BookCycleAction" $ \o -> do
    trade_id <- withRequired o "trade_id"
    cycle <- withRequired o "cycle"
    net_profit <- withRequired o "net_profit"
    gross_profit <- withRequired o "gross_profit"
    fees <- withRequired o "fees"
    settled_usd <- withDefault o "settled_usd" 0.0
    from_recovery <- withDefault o "from_recovery" False
    pure
      BookCycleAction
        { trade_id = trade_id,
          cycle = cycle,
          net_profit = net_profit,
          gross_profit = gross_profit,
          fees = fees,
          settled_usd = settled_usd,
          from_recovery = from_recovery
        }

data Action
  = ActPlaceOrder PlaceOrderAction
  | ActCancelOrder CancelOrderAction
  | ActOrphanOrder OrphanOrderAction
  | ActBookCycle BookCycleAction
  deriving (Eq, Show)

instance ToJSON Action where
  toJSON (ActPlaceOrder x) = toJSON x
  toJSON (ActCancelOrder x) = toJSON x
  toJSON (ActOrphanOrder x) = toJSON x
  toJSON (ActBookCycle x) = toJSON x

instance FromJSON Action where
  parseJSON = withObject "Action" $ \o -> do
    let has k = KM.member k o
    if has "local_id" && has "side" && has "role" && has "price" && has "volume" && has "trade_id" && has "cycle"
      then ActPlaceOrder <$> parseJSON (Object o)
      else
        if has "local_id" && has "recovery_id" && not (has "trade_id")
          then ActOrphanOrder <$> parseJSON (Object o)
          else
            if has "trade_id" && has "cycle" && has "net_profit" && has "gross_profit" && has "fees"
              then ActBookCycle <$> parseJSON (Object o)
              else
                if has "local_id" && has "txid"
                  then ActCancelOrder <$> parseJSON (Object o)
                  else fail "Unable to decode Action from payload fields"

normalizeRegimeId :: Value -> Maybe Int
normalizeRegimeId raw = case raw of
  Null -> Nothing
  Number _ ->
    case fromJSON raw of
      Success d -> normalizeRegimeInt (truncate (d :: Double))
      Error _ -> Nothing
  String t ->
    let s = T.strip t
     in if T.null s
          then Nothing
          else
            case T.toUpper s of
              "BEARISH" -> Just 0
              "RANGING" -> Just 1
              "BULLISH" -> Just 2
              _ ->
                if T.all isDigit s
                  then case TR.decimal s of
                    Right (value, rest) | T.null rest -> normalizeRegimeInt value
                    _ -> Nothing
                  else Nothing
  _ -> Nothing
  where
    normalizeRegimeInt value
      | value `elem` [0, 1, 2] = Just value
      | otherwise = Nothing

withRequired :: FromJSON a => Object -> Key -> Parser a
withRequired o k = o .: k

withDefault :: FromJSON a => Object -> Key -> a -> Parser a
withDefault o k def = o .:? k .!= def
