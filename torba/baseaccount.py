import typing
from typing import Dict, Tuple, Type, Optional, Any
from twisted.internet import defer

from torba.mnemonic import Mnemonic
from torba.bip32 import PrivateKey, PubKey, from_extended_key_string
from torba.hash import double_sha256, aes_encrypt, aes_decrypt

if typing.TYPE_CHECKING:
    from torba import baseledger
    from torba import wallet as basewallet


class AddressManager:

    name: str

    __slots__ = 'account', 'public_key', 'chain_number'

    def __init__(self, account, public_key, chain_number):
        self.account = account
        self.public_key = public_key
        self.chain_number = chain_number

    @classmethod
    def from_dict(cls, account: 'BaseAccount', d: dict) \
            -> Tuple['AddressManager', 'AddressManager']:
        raise NotImplementedError

    @classmethod
    def to_dict(cls, receiving: 'AddressManager', change: 'AddressManager') -> Dict:
        d: Dict[str, Any] = {'name': cls.name}
        receiving_dict = receiving.to_dict_instance()
        if receiving_dict:
            d['receiving'] = receiving_dict
        change_dict = change.to_dict_instance()
        if change_dict:
            d['change'] = change_dict
        return d

    def to_dict_instance(self) -> Optional[dict]:
        raise NotImplementedError

    @property
    def db(self):
        return self.account.ledger.db

    def _query_addresses(self, limit: int = None, max_used_times: int = None, order_by=None):
        return self.db.get_addresses(
            self.account, self.chain_number, limit, max_used_times, order_by
        )

    def get_private_key(self, index: int) -> PrivateKey:
        raise NotImplementedError

    def get_max_gap(self) -> defer.Deferred:
        raise NotImplementedError

    def ensure_address_gap(self) -> defer.Deferred:
        raise NotImplementedError

    def get_address_records(self, limit: int = None, only_usable: bool = False) -> defer.Deferred:
        raise NotImplementedError

    @defer.inlineCallbacks
    def get_addresses(self, limit: int = None, only_usable: bool = False) -> defer.Deferred:
        records = yield self.get_address_records(limit=limit, only_usable=only_usable)
        defer.returnValue([r['address'] for r in records])

    @defer.inlineCallbacks
    def get_or_create_usable_address(self) -> defer.Deferred:
        addresses = yield self.get_addresses(limit=1, only_usable=True)
        if addresses:
            defer.returnValue(addresses[0])
        addresses = yield self.ensure_address_gap()
        defer.returnValue(addresses[0])


class HierarchicalDeterministic(AddressManager):
    """ Implements simple version of Bitcoin Hierarchical Deterministic key management. """

    name = "deterministic-chain"

    __slots__ = 'gap', 'maximum_uses_per_address'

    def __init__(self, account: 'BaseAccount', chain: int, gap: int, maximum_uses_per_address: int) -> None:
        super().__init__(account, account.public_key.child(chain), chain)
        self.gap = gap
        self.maximum_uses_per_address = maximum_uses_per_address

    @classmethod
    def from_dict(cls, account: 'BaseAccount', d: dict) -> Tuple[AddressManager, AddressManager]:
        return (
            cls(account, 0, **d.get('receiving', {'gap': 20, 'maximum_uses_per_address': 2})),
            cls(account, 1, **d.get('change', {'gap': 6, 'maximum_uses_per_address': 2}))
        )

    def to_dict_instance(self):
        return {'gap': self.gap, 'maximum_uses_per_address': self.maximum_uses_per_address}

    def get_private_key(self, index: int) -> PrivateKey:
        return self.account.private_key.child(self.chain_number).child(index)

    @defer.inlineCallbacks
    def generate_keys(self, start: int, end: int) -> defer.Deferred:
        new_keys = []
        for index in range(start, end+1):
            new_keys.append((index, self.public_key.child(index)))
        yield self.db.add_keys(
            self.account, self.chain_number, new_keys
        )
        defer.returnValue([key[1].address for key in new_keys])

    @defer.inlineCallbacks
    def get_max_gap(self) -> defer.Deferred:
        addresses = yield self._query_addresses(order_by="position ASC")
        max_gap = 0
        current_gap = 0
        for address in addresses:
            if address['used_times'] == 0:
                current_gap += 1
            else:
                max_gap = max(max_gap, current_gap)
                current_gap = 0
        defer.returnValue(max_gap)

    @defer.inlineCallbacks
    def ensure_address_gap(self) -> defer.Deferred:
        addresses = yield self._query_addresses(self.gap, None, "position DESC")

        existing_gap = 0
        for address in addresses:
            if address['used_times'] == 0:
                existing_gap += 1
            else:
                break

        if existing_gap == self.gap:
            defer.returnValue([])

        start = addresses[0]['position']+1 if addresses else 0
        end = start + (self.gap - existing_gap)
        new_keys = yield self.generate_keys(start, end-1)
        defer.returnValue(new_keys)

    def get_address_records(self, limit: int = None, only_usable: bool = False):
        return self._query_addresses(
            limit, self.maximum_uses_per_address if only_usable else None,
            "used_times ASC, position ASC"
        )


class SingleKey(AddressManager):
    """ Single Key address manager always returns the same address for all operations. """

    name = "single-address"

    __slots__ = ()

    @classmethod
    def from_dict(cls, account: 'BaseAccount', d: dict)\
            -> Tuple[AddressManager, AddressManager]:
        same_address_manager = cls(account, account.public_key, 0)
        return same_address_manager, same_address_manager

    def to_dict_instance(self):
        return None

    def get_private_key(self, index: int) -> PrivateKey:
        return self.account.private_key

    def get_max_gap(self) -> defer.Deferred:
        return defer.succeed(0)

    @defer.inlineCallbacks
    def ensure_address_gap(self) -> defer.Deferred:
        exists = yield self.get_address_records()
        if not exists:
            yield self.db.add_keys(
                self.account, self.chain_number, [(0, self.public_key)]
            )
            defer.returnValue([self.public_key.address])
        defer.returnValue([])

    def get_address_records(self, limit: int = None, only_usable: bool = False) -> defer.Deferred:
        return self._query_addresses()


class BaseAccount:

    mnemonic_class = Mnemonic
    private_key_class = PrivateKey
    public_key_class = PubKey
    address_generators: Dict[str, Type[AddressManager]] = {
        SingleKey.name: SingleKey,
        HierarchicalDeterministic.name: HierarchicalDeterministic,
    }

    def __init__(self, ledger: 'baseledger.BaseLedger', wallet: 'basewallet.Wallet', name: str,
                 seed: str, encrypted: bool, private_key: PrivateKey, public_key: PubKey,
                 address_generator: dict) -> None:
        self.ledger = ledger
        self.wallet = wallet
        self.name = name
        self.seed = seed
        self.encrypted = encrypted
        self.private_key = private_key
        self.public_key = public_key
        generator_name = address_generator.get('name', HierarchicalDeterministic.name)
        self.address_generator = self.address_generators[generator_name]
        self.receiving, self.change = self.address_generator.from_dict(self, address_generator)
        self.address_managers = {self.receiving, self.change}
        ledger.add_account(self)
        wallet.add_account(self)

    @classmethod
    def generate(cls, ledger: 'baseledger.BaseLedger', wallet: 'basewallet.Wallet',
                 name: str = None, address_generator: dict = None):
        return cls.from_dict(ledger, wallet, {
            'name': name,
            'seed': cls.mnemonic_class().make_seed(),
            'address_generator': address_generator or {}
        })

    @classmethod
    def get_private_key_from_seed(cls, ledger: 'baseledger.BaseLedger', seed: str, password: str):
        return cls.private_key_class.from_seed(
            ledger, cls.mnemonic_class.mnemonic_to_seed(seed, password)
        )

    @classmethod
    def from_dict(cls, ledger: 'baseledger.BaseLedger', wallet: 'basewallet.Wallet', d: dict):
        seed = d.get('seed', '')
        private_key = d.get('private_key', '')
        public_key = None
        encrypted = d.get('encrypted', False)
        if not encrypted:
            if seed:
                private_key = cls.get_private_key_from_seed(ledger, seed, '')
                public_key = private_key.public_key
            elif private_key:
                private_key = from_extended_key_string(ledger, private_key)
                public_key = private_key.public_key
        if public_key is None:
            public_key = from_extended_key_string(ledger, d['public_key'])
        name = d.get('name')
        if not name:
            name = 'Account #{}'.format(public_key.address)
        return cls(
            ledger=ledger,
            wallet=wallet,
            name=name,
            seed=seed,
            encrypted=encrypted,
            private_key=private_key,
            public_key=public_key,
            address_generator=d.get('address_generator', {})
        )

    def to_dict(self):
        private_key = self.private_key
        if not self.encrypted and self.private_key:
            private_key = self.private_key.extended_key_string()
        return {
            'ledger': self.ledger.get_id(),
            'name': self.name,
            'seed': self.seed,
            'encrypted': self.encrypted,
            'private_key': private_key,
            'public_key': self.public_key.extended_key_string(),
            'address_generator': self.address_generator.to_dict(self.receiving, self.change)
        }

    def decrypt(self, password):
        assert self.encrypted, "Key is not encrypted."
        secret = double_sha256(password)
        self.seed = aes_decrypt(secret, self.seed)
        self.private_key = from_extended_key_string(self.ledger, aes_decrypt(secret, self.private_key))
        self.encrypted = False

    def encrypt(self, password):
        assert not self.encrypted, "Key is already encrypted."
        secret = double_sha256(password)
        self.seed = aes_encrypt(secret, self.seed)
        self.private_key = aes_encrypt(secret, self.private_key.extended_key_string())
        self.encrypted = True

    @defer.inlineCallbacks
    def ensure_address_gap(self):
        addresses = []
        for address_manager in self.address_managers:
            new_addresses = yield address_manager.ensure_address_gap()
            addresses.extend(new_addresses)
        defer.returnValue(addresses)

    @defer.inlineCallbacks
    def get_addresses(self, limit: int = None, max_used_times: int = None) -> defer.Deferred:
        records = yield self.get_address_records(limit, max_used_times)
        defer.returnValue([r['address'] for r in records])

    def get_address_records(self, limit: int = None, max_used_times: int = None) -> defer.Deferred:
        return self.ledger.db.get_addresses(self, None, limit, max_used_times)

    def get_private_key(self, chain: int, index: int) -> PrivateKey:
        assert not self.encrypted, "Cannot get private key on encrypted wallet account."
        address_manager = {0: self.receiving, 1: self.change}[chain]
        return address_manager.get_private_key(index)

    def get_balance(self, confirmations: int = 6, **constraints):
        if confirmations > 0:
            height = self.ledger.headers.height - (confirmations-1)
            constraints.update({'height__lte': height, 'height__gt': 0})
        return self.ledger.db.get_balance_for_account(self, **constraints)

    @defer.inlineCallbacks
    def get_max_gap(self):
        change_gap = yield self.change.get_max_gap()
        receiving_gap = yield self.receiving.get_max_gap()
        defer.returnValue({
            'max_change_gap': change_gap,
            'max_receiving_gap': receiving_gap,
        })

    def get_unspent_outputs(self, **constraints):
        return self.ledger.db.get_utxos_for_account(self, **constraints)

    @defer.inlineCallbacks
    def fund(self, to_account, amount=None, everything=False,
             outputs=1, broadcast=False, **constraints):
        assert self.ledger == to_account.ledger, 'Can only transfer between accounts of the same ledger.'
        tx_class = self.ledger.transaction_class
        if everything:
            utxos = yield self.get_unspent_outputs(**constraints)
            yield self.ledger.reserve_outputs(utxos)
            tx = yield tx_class.create(
                inputs=[tx_class.input_class.spend(txo) for txo in utxos],
                outputs=[],
                funding_accounts=[self],
                change_account=to_account
            )
        elif amount > 0:
            to_address = yield to_account.change.get_or_create_usable_address()
            to_hash160 = to_account.ledger.address_to_hash160(to_address)
            tx = yield tx_class.create(
                inputs=[],
                outputs=[
                    tx_class.output_class.pay_pubkey_hash(amount//outputs, to_hash160)
                    for _ in range(outputs)
                ],
                funding_accounts=[self],
                change_account=self
            )
        else:
            raise ValueError('An amount is required.')

        if broadcast:
            yield self.ledger.broadcast(tx)
        else:
            yield self.ledger.release_outputs(
                [txi.txo_ref.txo for txi in tx.inputs]
            )

        defer.returnValue(tx)
