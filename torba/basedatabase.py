import logging
from typing import Tuple, List, Sequence

import sqlite3
from twisted.internet import defer
from twisted.enterprise import adbapi

from torba.hash import TXRefImmutable

log = logging.getLogger(__name__)


def constraints_to_sql(constraints, joiner=' AND ', prepend_sql=' AND ', prepend_key=''):
    if not constraints:
        return ''
    extras = []
    for key in list(constraints):
        col, op = key, '='
        if key.endswith('__not'):
            col, op = key[:-len('__not')], '!='
        elif key.endswith('__lt'):
            col, op = key[:-len('__lt')], '<'
        elif key.endswith('__lte'):
            col, op = key[:-len('__lte')], '<='
        elif key.endswith('__gt'):
            col, op = key[:-len('__gt')], '>'
        elif key.endswith('__like'):
            col, op = key[:-len('__like')], 'LIKE'
        elif key.endswith('__any'):
            subconstraints = constraints.pop(key)
            extras.append('({})'.format(
                constraints_to_sql(subconstraints, ' OR ', '', key+'_')
            ))
            for subkey, val in subconstraints.items():
                constraints['{}_{}'.format(key, subkey)] = val
            continue
        extras.append('{} {} :{}'.format(col, op, prepend_key+key))
    return prepend_sql + joiner.join(extras) if extras else ''


class SQLiteMixin:

    CREATE_TABLES_QUERY: Sequence[str] = ()

    def __init__(self, path):
        self._db_path = path
        self.db: adbapi.ConnectionPool = None

    def open(self):
        log.info("connecting to database: %s", self._db_path)
        self.db = adbapi.ConnectionPool(
            'sqlite3', self._db_path, cp_min=1, cp_max=1, check_same_thread=False
        )
        return self.db.runInteraction(
            lambda t: t.executescript(self.CREATE_TABLES_QUERY)
        )

    def close(self):
        self.db.close()
        return defer.succeed(True)

    @staticmethod
    def _insert_sql(table: str, data: dict) -> Tuple[str, List]:
        columns, values = [], []
        for column, value in data.items():
            columns.append(column)
            values.append(value)
        sql = "INSERT INTO {} ({}) VALUES ({})".format(
            table, ', '.join(columns), ', '.join(['?'] * len(values))
        )
        return sql, values

    @staticmethod
    def _update_sql(table: str, data: dict, where: str, constraints: list) -> Tuple[str, list]:
        columns, values = [], []
        for column, value in data.items():
            columns.append("{} = ?".format(column))
            values.append(value)
        values.extend(constraints)
        sql = "UPDATE {} SET {} WHERE {}".format(
            table, ', '.join(columns), where
        )
        return sql, values

    @defer.inlineCallbacks
    def query_one_value(self, query, params=None, default=None):
        result = yield self.run_query(query, params)
        if result:
            defer.returnValue(result[0][0] or default)
        else:
            defer.returnValue(default)

    @defer.inlineCallbacks
    def query_dict_value_list(self, query, fields, params=None):
        result = yield self.run_query(query.format(', '.join(fields)), params)
        if result:
            defer.returnValue([dict(zip(fields, r)) for r in result])
        else:
            defer.returnValue([])

    @defer.inlineCallbacks
    def query_dict_value(self, query, fields, params=None, default=None):
        result = yield self.query_dict_value_list(query, fields, params)
        if result:
            defer.returnValue(result[0])
        else:
            defer.returnValue(default)

    @staticmethod
    def execute(t, sql, values):
        log.debug(sql)
        log.debug(values)
        return t.execute(sql, values)

    def run_operation(self, sql, values):
        log.debug(sql)
        log.debug(values)
        return self.db.runOperation(sql, values)

    def run_query(self, sql, values):
        log.debug(sql)
        log.debug(values)
        return self.db.runQuery(sql, values)


class BaseDatabase(SQLiteMixin):

    CREATE_PUBKEY_ADDRESS_TABLE = """
        create table if not exists pubkey_address (
            address text primary key,
            account text not null,
            chain integer not null,
            position integer not null,
            pubkey blob not null,
            history text,
            used_times integer not null default 0
        );
    """

    CREATE_TX_TABLE = """
        create table if not exists tx (
            txid text primary key,
            raw blob not null,
            height integer not null,
            is_verified boolean not null default 0
        );
    """

    CREATE_TXO_TABLE = """
        create table if not exists txo (
            txid text references tx,
            txoid text primary key,
            address text references pubkey_address,
            position integer not null,
            amount integer not null,
            script blob not null,
            is_reserved boolean not null default 0
        );
    """

    CREATE_TXI_TABLE = """
        create table if not exists txi (
            txid text references tx,
            txoid text references txo,
            address text references pubkey_address
        );
    """

    CREATE_TABLES_QUERY = (
        CREATE_TX_TABLE +
        CREATE_PUBKEY_ADDRESS_TABLE +
        CREATE_TXO_TABLE +
        CREATE_TXI_TABLE
    )

    @staticmethod
    def txo_to_row(tx, address, txo):
        return {
            'txid': tx.id,
            'txoid': txo.id,
            'address': address,
            'position': txo.position,
            'amount': txo.amount,
            'script': sqlite3.Binary(txo.script.source)
        }

    def save_transaction_io(self, save_tx, tx, height, is_verified, address, txhash, history):

        def _steps(t):
            if save_tx == 'insert':
                self.execute(t, *self._insert_sql('tx', {
                    'txid': tx.id,
                    'raw': sqlite3.Binary(tx.raw),
                    'height': height,
                    'is_verified': is_verified
                }))
            elif save_tx == 'update':
                self.execute(t, *self._update_sql("tx", {
                    'height': height, 'is_verified': is_verified
                }, 'txid = ?', (tx.id,)))

            existing_txos = [r[0] for r in self.execute(
                t, "SELECT position FROM txo WHERE txid = ?", (tx.id,)
            ).fetchall()]

            for txo in tx.outputs:
                if txo.position in existing_txos:
                    continue
                if txo.script.is_pay_pubkey_hash and txo.script.values['pubkey_hash'] == txhash:
                    self.execute(t, *self._insert_sql("txo", self.txo_to_row(tx, address, txo)))
                elif txo.script.is_pay_script_hash:
                    # TODO: implement script hash payments
                    print('Database.save_transaction_io: pay script hash is not implemented!')

            # lookup the address associated with each TXI (via its TXO)
            txoids = [txi.txo_ref.id for txi in tx.inputs]
            txoid_place_holders = ','.join(['?']*len(txoids))
            txoid_to_address = {r[0]: r[1] for r in self.execute(
                t, "SELECT txoid, address FROM txo WHERE txoid in ({})".format(txoid_place_holders), txoids
            ).fetchall()}

            # list of TXIs that have already been added
            existing_txis = [r[0] for r in self.execute(
                t, "SELECT txoid FROM txi WHERE txid = ?", (tx.id,)
            ).fetchall()]

            for txi in tx.inputs:
                txoid = txi.txo_ref.id
                new_txi = txoid not in existing_txis
                address_matches = txoid_to_address.get(txoid) == address
                if new_txi and address_matches:
                    self.execute(t, *self._insert_sql("txi", {
                        'txid': tx.id,
                        'txoid': txoid,
                        'address': address,
                    }))

            self._set_address_history(t, address, history)

        return self.db.runInteraction(_steps)

    def reserve_outputs(self, txos, is_reserved=True):
        txoids = [txo.id for txo in txos]
        return self.run_operation(
            "UPDATE txo SET is_reserved = ? WHERE txoid IN ({})".format(
                ', '.join(['?']*len(txoids))
            ), [is_reserved]+txoids
        )

    def release_outputs(self, txos):
        return self.reserve_outputs(txos, is_reserved=False)

    def rewind_blockchain(self, above_height):  # pylint: disable=no-self-use
        # TODO:
        # 1. delete transactions above_height
        # 2. update address histories removing deleted TXs
        return defer.succeed(True)

    @defer.inlineCallbacks
    def get_transaction(self, txid):
        result = yield self.run_query(
            "SELECT raw, height, is_verified FROM tx WHERE txid = ?", (txid,)
        )
        if result:
            defer.returnValue(result[0])
        else:
            defer.returnValue((None, None, False))

    def get_balance_for_account(self, account, include_reserved=False, **constraints):
        if not include_reserved:
            constraints['is_reserved'] = 0
        values = {'account': account.public_key.address}
        values.update(constraints)
        return self.query_one_value(
            """
            SELECT SUM(amount)
            FROM txo
                JOIN tx ON tx.txid=txo.txid
                JOIN pubkey_address ON pubkey_address.address=txo.address
            WHERE
              pubkey_address.account=:account AND
              txoid NOT IN (SELECT txoid FROM txi)
            """+constraints_to_sql(constraints), values, 0
        )

    @defer.inlineCallbacks
    def get_utxos_for_account(self, account, **constraints):
        constraints['account'] = account.public_key.address
        utxos = yield self.run_query(
            """
            SELECT amount, script, txid, txo.position
            FROM txo JOIN pubkey_address ON pubkey_address.address=txo.address
            WHERE account=:account AND txo.is_reserved=0 AND txoid NOT IN (SELECT txoid FROM txi)
            """+constraints_to_sql(constraints), constraints
        )
        output_class = account.ledger.transaction_class.output_class
        defer.returnValue([
            output_class(
                values[0],
                output_class.script_class(values[1]),
                TXRefImmutable.from_id(values[2]),
                position=values[3]
            ) for values in utxos
        ])

    def add_keys(self, account, chain, keys):
        sql = (
            "insert into pubkey_address "
            "(address, account, chain, position, pubkey) "
            "values "
        ) + ', '.join(['(?, ?, ?, ?, ?)'] * len(keys))
        values = []
        for position, pubkey in keys:
            values.append(pubkey.address)
            values.append(account.public_key.address)
            values.append(chain)
            values.append(position)
            values.append(sqlite3.Binary(pubkey.pubkey_bytes))
        return self.run_operation(sql, values)

    @classmethod
    def _set_address_history(cls, t, address, history):
        cls.execute(
            t, "UPDATE pubkey_address SET history = ?, used_times = ? WHERE address = ?",
            (history, history.count(':')//2, address)
        )

    def set_address_history(self, address, history):
        return self.db.runInteraction(lambda t: self._set_address_history(t, address, history))

    def get_addresses(self, account, chain, limit=None, max_used_times=None, order_by=None):
        columns = ['account', 'chain', 'position', 'address', 'used_times']
        sql = ["SELECT {} FROM pubkey_address"]

        where = []
        params = {}
        if account is not None:
            params["account"] = account.public_key.address
            where.append("account = :account")
            columns.remove("account")
        if chain is not None:
            params["chain"] = chain
            where.append("chain = :chain")
            columns.remove("chain")
        if max_used_times is not None:
            params["used_times"] = max_used_times
            where.append("used_times <= :used_times")

        if where:
            sql.append("WHERE")
            sql.append(" AND ".join(where))

        if order_by:
            sql.append("ORDER BY {}".format(order_by))

        if limit is not None:
            sql.append("LIMIT {}".format(limit))

        return self.query_dict_value_list(" ".join(sql), columns, params)

    def get_address(self, address):
        return self.query_dict_value(
            "SELECT {} FROM pubkey_address WHERE address = :address",
            ('address', 'account', 'chain', 'position', 'pubkey', 'history', 'used_times'),
            {'address': address}
        )
