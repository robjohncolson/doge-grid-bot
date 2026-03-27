{-# LANGUAGE OverloadedStrings #-}

module Main where

import Control.Monad (foldM)
import Data.Aeson qualified as A
import Data.Aeson.Key qualified as K
import Data.Aeson.KeyMap qualified as KM
import Data.ByteString qualified as BS
import Data.ByteString.Char8 qualified as BSC
import Data.ByteString.Lazy qualified as BL
import Data.Text qualified as T
import DogeCore.Invariants (checkInvariants)
import DogeCore.Transition (nativeTransition)
import DogeCore.Types (Action, EngineConfig, Event (..), OrderState (..), PairState (..), TradeId (..), normalizeRegimeId)
import System.Environment (getArgs)
import System.Exit (ExitCode (..), exitWith)
import System.IO (hFlush, hPutStr, isEOF, stderr, stdout)

main :: IO ()
main = do
  args <- getArgs
  if "--server" `elem` args
    then runServer
    else runSingle

runSingle :: IO ()
runSingle = do
  raw <- BS.getContents
  case decodeRequest raw >>= handleRequest of
    Left err -> failWith 2 err
    Right response -> BL.putStr response

runServer :: IO ()
runServer = loop
  where
    loop = do
      eof <- isEOF
      if eof
        then pure ()
        else do
          raw <- BSC.getLine
          let response = either renderError id (decodeRequest raw >>= handleRequest)
          BL.putStr response
          BSC.putStr "\n"
          hFlush stdout
          loop

decodeRequest :: BS.ByteString -> Either String A.Object
decodeRequest raw = case A.eitherDecodeStrict' raw of
  Left err -> Left ("invalid JSON request: " <> err)
  Right (A.Object obj) -> Right obj
  Right _ -> Left "request must be a JSON object"

handleRequest :: A.Object -> Either String BL.ByteString
handleRequest obj = case KM.lookup "method" obj of
  Just (A.String "check_invariants") -> handleCheckInvariants obj
  Just (A.String "transition") -> handleTransition obj
  Just (A.String "apply_order_regime_at_entry") -> handleApplyOrderRegimeAtEntry obj
  Just (A.String methodName) -> Left ("unsupported method: " <> T.unpack methodName)
  _ -> Left "missing method field"

handleCheckInvariants :: A.Object -> Either String BL.ByteString
handleCheckInvariants obj = do
  state <- parseRequired "state" obj
  let response = A.object ["violations" A..= checkInvariants (state :: PairState)]
  Right (A.encode response)

handleTransition :: A.Object -> Either String BL.ByteString
handleTransition obj = do
  (state, event, cfg, orderSizeUsd, orderSizes) <- parseTransitionRequest obj
  let (nextState, actions) = nativeTransition state event cfg orderSizeUsd orderSizes
  Right (encodeTransitionResponse nextState actions)

handleApplyOrderRegimeAtEntry :: A.Object -> Either String BL.ByteString
handleApplyOrderRegimeAtEntry obj = do
  payloadObj <- parseParamsObject obj
  state <- parseRequired "state" payloadObj
  localId <- parseRequired "local_id" payloadObj
  regimeRaw <- parseRequired "regime_at_entry" payloadObj
  let normalizedRegime = normalizeRegimeId regimeRaw
      patchedOrders =
        map
          (\order@OrderState {local_id = orderLocalId} -> if orderLocalId == localId then order {regime_at_entry = normalizedRegime} else order)
          (orders state)
      patchedState = state {orders = patchedOrders}
  Right (A.encode (A.object ["state" A..= patchedState]))

parseTransitionRequest :: A.Object -> Either String (PairState, Event, EngineConfig, Double, Maybe [(TradeId, Double)])
parseTransitionRequest obj = do
  state <- parseRequired "state" obj
  event <- parseRequired "event" obj
  cfg <- parseRequired "config" obj
  orderSizeUsd <- parseRequired "order_size_usd" obj
  orderSizes <- parseOrderSizes obj
  pure (state, event, cfg, orderSizeUsd, orderSizes)

parseParamsObject :: A.Object -> Either String A.Object
parseParamsObject obj = case KM.lookup "params" obj of
  Nothing -> Right obj
  Just A.Null -> Left "invalid params payload: must be an object"
  Just (A.Object payloadObj) -> Right payloadObj
  Just _ -> Left "invalid params payload: must be an object"

parseRequired :: A.FromJSON a => K.Key -> A.Object -> Either String a
parseRequired key obj = case KM.lookup key obj of
  Nothing -> Left ("missing " <> T.unpack (K.toText key) <> " field")
  Just rawValue -> case A.fromJSON rawValue of
    A.Error err -> Left ("invalid " <> T.unpack (K.toText key) <> " payload: " <> err)
    A.Success value -> Right value

parseOrderSizes :: A.Object -> Either String (Maybe [(TradeId, Double)])
parseOrderSizes obj = case KM.lookup "order_sizes" obj of
  Nothing -> Right Nothing
  Just A.Null -> Right Nothing
  Just (A.Object orderSizesObj) -> Just <$> foldM parseOne [] (KM.toList orderSizesObj)
  Just _ -> Left "invalid order_sizes payload: must be an object or null"
  where
    parseOne acc (key, rawValue) = do
      tradeId <- case keyToTradeId key of
        Nothing -> Left ("invalid order_sizes key: " <> T.unpack (K.toText key))
        Just value -> Right value
      size <- case A.fromJSON rawValue of
        A.Error err -> Left ("invalid order_sizes value for " <> T.unpack (K.toText key) <> ": " <> err)
        A.Success value -> Right value
      Right ((tradeId, size) : acc)

keyToTradeId :: K.Key -> Maybe TradeId
keyToTradeId key = case T.toUpper (K.toText key) of
  "A" -> Just TradeA
  "B" -> Just TradeB
  _ -> Nothing

encodeTransitionResponse :: PairState -> [Action] -> BL.ByteString
encodeTransitionResponse nextState actions =
  A.encode (A.object ["state" A..= nextState, "actions" A..= actions])

renderError :: String -> BL.ByteString
renderError msg = A.encode (A.object ["error" A..= msg])

failWith :: Int -> String -> IO a
failWith code msg = do
  hPutStr stderr (msg <> "\n")
  exitWith (ExitFailure code)
