from twisted.internet import reactor, defer, threads, task
from twisted.internet.protocol import DatagramProtocol, Protocol, ClientFactory
import uuid
import logging
import netifaces as ni
import time
import time_uuid
import pickle
import random
from iwant.config import SERVER_DAEMON_HOST, SERVER_DAEMON_PORT
from iwant.constants.election_constants import (
        MCAST_IP,MCAST_PORT,
        NEW_PEER,RE_ELECTION,
        ALIVE,BCAST_LEDGER,HANDLE_PING,
        HANDLE_ALIVE,NEW_LEADER,
        HANDLE_PONG,REMOVE_LEADER,
        PING, PONG, FACE_OFF, SECRET_VAL
    )
from iwant.constants.server_event_constants import LEADER
from iwant.communication.message import P2PMessage
from iwant.communication.election_communication.message import *
from iwant.utils.utils import generate_secret, generate_size, EventHooker
from iwant.protocols import ServerElectionProtocol, ServerElectionFactory, PeerdiscoveryProtocol

MCAST_ADDR = (MCAST_IP, MCAST_PORT)



#class PeerdiscoveryProtocol(DatagramProtocol):
#
#    def escape_hash_sign(self, string):
#        return string.replace(self.delimiter, '')
#
#    def _process_msg(self, req, addr):
#        pass
#
#    def send(self, msgObj, addr):
#        self.transport.write(str(msgObj), addr)
#
#    def datagramReceived(self, datagram, addr):
#        for dat in datagram:
#            self.buff += dat
#            if dat == self.delimiter:
#                req_str = self.escape_hash_sign(self.buff)
#                self.buff = ''
#                self._process_msg(req_str, addr)
#        self.buff = ''


#class CommonlogBook(object):
#    __doc__ = '''
#        This is a book which will store all the persistent data,
#        for example, peers,leader,state,uuid
#    '''
#
#    def __init__(self, identity=None, state=None, peers={}, leader=None, ip=None):
#        """
#        :param identity: uuid representing the identity of the peer
#        :param state: defines the state of the peer
#        :param peers: empty peers list which will be updated
#        :param ip: ip of the peer
#        """
#        self.state = state
#        self.peers = peers
#        self.leader = None  # uuid
#        self.isLeader = False  # its better to use this than keep comparing uuid objects
#        self.uuid = identity.hex
#        self.ip = ip
#        self.uuidObj = identity


#class EventHooker(object):
#    __doc__ = """
#        Registering custom event callbacks
#    """
#    def __init__(self):
#        self.events = {}
#
#    def bind(self,event,callback):
#        '''
#         Registers callbacks to an event
#        :param event : string
#        :param callback : function
#        '''
#        self.events[event] = callback
#
#    def unbind(self,event):
#        '''
#        Detach events from the hooker
#        :param event: string
#        '''
#        if event in self.events:
#            del self.events[event]

class CommonroomProtocol(PeerdiscoveryProtocol):
    __doc__ = '''
        Commonroom multicasts the winner
        Commonroom multicasts its ID
    '''
    maxDelay = 3600
    initialDelay = 1.0
    factor = 2.7182818284590451
    delay = initialDelay
    retries = 0
    maxRetries = 3
    _callID = None
    clock = None
    continueTrying = 1

    def __init__(self, book):
        '''
            build the message codes
            :param book: CommonLogBook instance
        '''
        self.book = book
        self.secret_value = None
        #self.message_codes = {  # Haven't used it properly yet
        #    0: self._new_peers,  # used named keys [ have event regsiters ]
        #    1: self._re_election_event,
        #    2: self._alive,
        #    3: self._manage_ledger,
        #    4: self._handle_ping,
        #    5: self._alive_handler,
        #    6: self._new_leader_callback,
        #    7: self._handle_pong,
        #    8: self._remove_leader
        #}
        self.eventcontroller = EventHooker()
        self.eventcontroller.bind(NEW_PEER, self._new_peers)
        self.eventcontroller.bind(RE_ELECTION, self._re_election_event)
        self.eventcontroller.bind(ALIVE, self._alive)
        self.eventcontroller.bind(BCAST_LEDGER, self._manage_ledger)
        self.eventcontroller.bind(HANDLE_PING, self._handle_ping)
        self.eventcontroller.bind(HANDLE_ALIVE, self._alive_handler)
        self.eventcontroller.bind(NEW_LEADER, self._new_leader_callback)
        self.eventcontroller.bind(HANDLE_PONG, self._handle_pong)
        self.eventcontroller.bind(REMOVE_LEADER, self._remove_leader)
        self.eventcontroller.bind(FACE_OFF, self._face_off)

        self._none_alive_ack = 1  # when no peers are present
        self._eln_ack = None  # waiting for election ack
        self._ping_ack = 0  # waiting for failure
        self._eClock = None
        self._alClock = None
        self._pollClock = None
        self._npClock = None  # new peer clock
        self._npCallId = None  # new peer call id
        self._eCallId = None
        self._alCallId = None
        self._pollId = None
        self._eid = None
        self._addr = (self.book.ip, MCAST_PORT)
        self._latest_election_id = None
        self.buff = ''
        self.delimiter = '#'
        print 'ID : ' + self.book.uuid

    def cancel_wait_for_peers_callback(self):
        if self._npCallId is not None:
            if self._npCallId.active():
                self._npCallId.cancel()

    def cancel_election_callback(self):
        if self._eCallId is not None:
            if self._eCallId.active():
                self._eCallId.cancel()

    def cancel_alive_callback(self):
        if self._alCallId is not None:
            if self._alCallId.active():
                self._alCallId.cancel()

    @staticmethod
    def generate_election_id():  # Make it static
        return time.time()

    def startProtocol(self):
        """
        Join the multicast group and announce the identity
        and decide to become the leader if there is no response
        """
        self.book.peers[self.book.uuidObj] = self._addr
        self.transport.setTTL(5)
        self.transport.joinGroup(MCAST_ADDR[0])
        self._broadcast_identity()
        wait_for_response = 3

        def response_from_peers():
            self._eid = self.generate_election_id()
            self._leader(leader=self.book.uuidObj, eid=self._eid)

        if self._npClock is None:
            from twisted.internet import reactor
            self._npClock = reactor
        self._npCallId = self._npClock.callLater(wait_for_response, response_from_peers)
        self.d = threads.deferToThread(self._poll)  # ideally, we should register a callback

    def _broadcast_identity(self):
        self.send(FlashMessage(NEW_PEER, [self.book.uuidObj, self.book.leader]), MCAST_ADDR)

    def _broadcast_leader_dead(self):
        print 'broadcasting coz leader is dead'
        self.send(FlashMessage(REMOVE_LEADER, [self.book.leader]), MCAST_ADDR)
        self._broadcast_re_election()

    def _broadcast_re_election(self):
        print 'BROADCASTIN RE ELECTION'
        eid = self.generate_election_id()
        self.send(FlashMessage(RE_ELECTION, [eid]), MCAST_ADDR)

    def _send_id_to(self, addr):
        self.send(FlashMessage(NEW_PEER, [self.book.uuidObj, self.book.leader]), addr)

    def _send_pong_to(self, addr):
        self.send(FlashMessage(HANDLE_PONG, [self.secret_value]), addr)

    def _broadcast_winner(self, eid):
        '''
            broadcasting winner message
        '''
        #print 'Sending SECRET {0}'.format(self.secret_value)
        self.send(FlashMessage(NEW_LEADER, [self.book.leader, eid, self.secret_value]), MCAST_ADDR)

    def _send_election_msg_to(self, pid):
        eid = self.generate_election_id()
        addr = self.book.peers[pid]
        self.send(FlashMessage(ALIVE, [eid]), addr)

    def _ping(self, addr):
        self.send(FlashMessage(HANDLE_PING, [PING]), addr)  # might be a problem

    def _send_alive_msg_to(self, addr):
        eid = self.generate_election_id()
        self.send(FlashMessage(HANDLE_ALIVE, [eid]), addr)

    def _broadcast_ledger(self, add_secret=False, bullying=None):
        ledger = self.book.peers
        if add_secret:
            # We add secret when we are sending the ledger to a new peer, so that the peer can ping the leader and expect the secret value
            self.send(FlashMessage(BCAST_LEDGER, [self.book.leader, ledger, self.secret_value]), MCAST_ADDR)
        elif bullying is not None:
            # This is necessary when the wrong winner is announced for the election. We then just exchange ledger with each other and organize a fresh new re-election
            self.send(FlashMessage(BCAST_LEDGER, [bullying, ledger]), MCAST_ADDR)
        else:
            # When split brain occurs, we exchange ledger and perform re-election
            # We cant really let all the peers to announce re-election. Lets make only the leaders of different clusters announce the re-election
            self.send(FlashMessage(BCAST_LEDGER, [self.book.leader, ledger]), MCAST_ADDR)

    def _send_secret_value(self, addr):
        '''
            Send secret value to addr only when a new peer enters
        '''
        self.send(FlashMessage(SECRET_VAL, [self.secret.value]), addr)

    def _send_face_off(self, addr):
        '''
            Send a face off message: same election id but different winners
        '''
        self.send(FlashMessage(FACE_OFF), addr)

    def isLeader(self):
        if self.book.leader == self.book.uuidObj:
            return True
        else:
            return False

    def leader_present(self):
        if self.book.leader:
            return True
        else:
            return False

    def _poll(self):
        '''
            Keep polling the server to test if its dead or alive
        '''
        if self._pollClock is None:
            from twisted.internet import reactor
            self._pollClock = reactor
        if self.book.leader != self.book.uuidObj and self.book.leader:
            def ping_callback():
                if not self._ping_ack:
                    self.retries += 1
                    if self.retries >= self.maxRetries:
                        print 'FAILED {0}'.format(self.retries)
                        self._broadcast_leader_dead()
                else:
                    self.retries = 0
                self._ping_ack = 0  # reset the ping_ack to 0

            # when leader is removed and we are present in this block, since the polling process occuring concurrently
            try:
                leader_addr = self.book.peers[self.book.leader]
                self._ping(leader_addr)
                self._pollClock.callLater(2, ping_callback)  # wait for 2 seconds to check if the leader replied
            except:
                pass

        self._pollId = self._pollClock.callLater(4, self._poll)  # ping the server every 4 seconds

    def periodic_reminder_from_leader(self):
        '''
            TODO: This is a kickass fucntion which will remind the peers about the leader periodicall and supply a new secret value
        '''
        if self.book.leader == self.book.uuidObj:
            self._remind_about_leader()
            reactor.callLater(6, self.periodic_reminder_from_leader)

    def _process_msg(self, req, addr):
        '''
            This acts as a router because it dispatches functions based on the event received
        '''
        msg = FlashMessage(message=req)
        if msg.key in [NEW_PEER, ALIVE, HANDLE_PING, NEW_LEADER]:
            self.eventcontroller.events[msg.key](data=msg.data, addr=addr)
        else:
            self.eventcontroller.events[msg.key](data=msg.data)

    def _new_peers(self, data=None, peer=None, leader=None, addr=None):
        '''
            Add new peers and decide whether to accept them as leaders or bully them
            :param data: represents a list containing peer and leader
            :param peer: represents the uuid of other peers
            :param leader: represents the uuid of the leader
            :param addr: (ip,port) of the new peer
        '''
        if data is not None:
            peer, leader = data

        if peer != self.book.uuidObj:
            self.cancel_wait_for_peers_callback()
            if peer not in self.book.peers:
                self.book.peers[peer] = addr
                if self.isLeader(): #self.book.leader == self.book.uuidObj:  # if leader, send the ledger
                    self._broadcast_ledger(add_secret=True)
                else:
                    if not leader and not self.leader_present(): #not self.book.leader:  # there are no leaders whatsoever
                        self._send_id_to(addr)
                        print ' no leader at all and then broadcast re-election'
                        self._broadcast_re_election()

    def _manage_ledger(self, data=None, ledger=None, leader=None):
        '''
            If there is a newcomer or if there are multiple leaders,
            send them an updated copy of the ledger containing all the peers in the network
        '''
        bullying = None

        if data is not None:
            try:
                value, ledger = data
                if type(value) == bool:
                    bullying = value  # yes or no for bullying
                else:
                    leader = value  # leader value

            except ValueError:
                #try:
                leader, ledger, secret = data  # for the new peer
                #except ValueError:
                #    ledger = data[0]  # we have to bully
                #    bullying = True

        self.cancel_wait_for_peers_callback()

        # Ledger updation
        temp_ledger = self.book.peers.copy()
        temp_ledger.update(ledger)
        self.book.peers = temp_ledger
        for key, value in self.book.peers.iteritems():
            #print  key, value
            pass

        if bullying is not None:
            '''
                Exchange ledger to update peers list and then bully due to wrong winner of election
            '''
            if bullying:
                self._bully()  # this will reduce the number of re-elections
            else:
                pass

        elif not self.leader_present():
            '''
                Informing NEW PEER about leader and secret value
            '''
            self.cancel_wait_for_peers_callback()
            self.secret_value = secret
            #print 'Accepting Secret {0}'.format(secret)
            self._new_leader_callback(leader=leader)

        elif self.isLeader() and leader and leader!=self.book.leader:
            '''
                Split brain scenario
                If leader value shared is different from the current leader
            '''
            self._new_leader_callback(leader=leader)

    def _handle_ping(self, data=None, addr=None):
        '''
            Along with pong, also append the secret value
        '''
        self._send_pong_to(addr)

    def _remove_leader(self, data=None, leader=None):
        """
            Todo: For removing leader, the announcing peer must also broadcast the previous electionId.
        """
        if data is not None:
            leader = data[0]
        if leader == self.book.leader and leader in self.book.peers:
            #print 'REMOVING LEADER: {0}'.format(leader)
            try:
                del self.book.peers[leader]
                self.book.leader = None
                self.secret_value = None
            except KeyError:
                pass

    def _handle_pong(self, data=None):
        '''
            Here we check if the value sent by the leader is equal to the secret value shared right after winning the election
        '''
        self._ping_ack = 1
        if data is not None:
            secret = data[0]
            print 'PONG : SECRET RECVD: {0}'.format(secret)
            if secret != self.secret_value:
                # broadcast leader is dead
                print 'MISMATCH WITH {0}'.format(self.secret_value)
                self._broadcast_leader_dead()

    def _re_election_event(self, data=None, eid=None):
        if data is not None:
            eid = data
        if self._eid is None:
            print 'NEW ELECTION COMMENCEMENT : {0}'.format(eid)
            self._eid = eid
            self._election(eid)
        else:
            if eid > self._eid:
                print 'CANCEL ELECTION {0}'.format(self._eid)
                self._eid = eid
                self.reset()
                self.cancel_election_callback()
                self.cancel_alive_callback()
                self._election(eid)

    def _election(self, eid):
        '''
            Sending election message to higher peers
            Every time there is an election reset the values of ack
        '''
        if self._eid == eid:
            requested_peers_list = filter(lambda x: x < self.book.uuidObj, self.book.peers.keys())
            for peer in requested_peers_list:
                self._send_election_msg_to(peer)

            self.delay = min(self.maxDelay, self.delay*self.factor)

            def election_callback(election_id):
                self._leader(self.book.uuidObj,election_id)

            if self._eClock is None:
                from twisted.internet import reactor
                self._eClock = reactor
            self._eCallId = self._eClock.callLater(self.delay, election_callback, eid)


    def _alive(self, data=None, eid=None, addr=None):
        '''
            Responding to the election message from lower Peer ID , I am alive.
        '''
        if data is not None:
            eid =  data
        if self._eid == eid:
            self._send_alive_msg_to(addr)

    def _alive_handler(self, data=None, eid=None):
        '''
            Gets an "Alive" message from higher peer.
            Will be waiting for the winner message now
        '''
        if data is not None:
            eid = data
        if self._eid == eid:
            self.cancel_election_callback()

            def wait_for_winner(no_response):
                if no_response:
                    print 'no response, broadcast re-election'
                    self._broadcast_re_election()

            alive_deferred = defer.Deferred()
            alive_deferred.addCallback(wait_for_winner)
            self.delay = min(self.maxDelay, self.delay*self.factor)
            if self._alClock is None:
                from twisted.internet import reactor
                self._alClock = reactor
            self._alCallId = self._alClock.callLater(self.delay, alive_deferred.callback, True)

    def _new_leader_callback(self, data=None, leader=None, eid=None, addr=None):
        '''
            This is a callback once the peers receive their new leader
            If there is a cluster which gets the result of a wrong election id ,
            then the leader of the cluster must broadcast the ledger.
        '''
        if data is not None:
            leader, eid, secret = data
            #print 'SECRET {0}'.format(secret)

        if eid == None:
            '''
                Post election time
            '''
            if self.book.uuidObj < leader:
                print 'about to bully {0} {1}'.format(self.book.uuidObj, leader)
                self._bully()
            elif self.isLeader():
                pass
            else:
                self.book.leader = leader
                print 'register leader {0}'.format(self.book.leader)
                self.notify_server()

                # Informing local server daemon about the new leader
                #leader_host = self.book.peers[self.book.leader][0]
                #leader_port = SERVER_DAEMON_PORT
                #factory = ServerElectionFactory(leader_host, leader_port)
                #reactor.connectTCP(SERVER_DAEMON_HOST, SERVER_DAEMON_PORT, factory)

        elif self._eid != eid:
            '''
                Random peer anounces itself as the winner and passes incorrect election ID
                last stage of election
            '''
            print 'WRONG ELEC ID {0} {1}'.format(self._eid,eid)
            if self.leader_present():
                if self.isLeader():
                    self._send_face_off(addr)
                    self._broadcast_ledger()  # two diff leaders will bcast their ledger
                elif self.retries > 0:
                    # Its possible that the leader is already dead , but the peers think that leader is alive
                    # based on the retries, we can slightly guess that the leader might not be present [Not a good way]
                    self._send_face_off(addr)  # request to tell the opposite peer to send its peers list , i.e , ledger
                    if self.book.uuidObj < leader:
                        self._broadcast_ledger(bullying=True)
                    else:
                        self._broadcast_ledger(bullying=False)
            else:
                self._send_face_off(addr)
                self._broadcast_ledger(bullying=True)

        elif self._eid == eid:
            '''
                Last stage of election
            '''
            #if self.book.uuidObj == self.book.leader and self.book.uuidObj != leader:
            #    '''
            #        Same election ID but different winners
            #    '''
            #    self._send_face_off(addr)
            #    # lets have a face off
            #    pass
            #else:
            print 'LEADER :{0}\t EID : {1}'.format(self.book.leader,self._eid)
            self.book.leader = leader
            self.secret_value = secret
            self.cancel_election_callback()
            self.cancel_alive_callback()
            if self.book.uuidObj < self.book.leader:
                """
                 2 Scenarios are possible:
                 1.The new peer has a higher id than leader but doesn't know anyone
                 therefore , he will broadcast his id , get the ledger and contest election
                 2.The new peer announces himself as the peer. This will lead to all the remaining
                 peers to broadcast_identity.
                """
                #self._broadcast_identity()
                self._send_face_off(addr)
                self._broadcast_ledger(bullying=True)

            print 'CLOSING ELECTION: {0}'.format(self._eid)
            self._latest_election_id = self._eid
            self._eid = None  # this might create a huge problem
            self.notify_server()

            #leader_host = self.book.peers[self.book.leader][0]
            #leader_port = SERVER_DAEMON_PORT
            #factory = ServerElectionFactory(leader_host, leader_port)
            #reactor.connectTCP(SERVER_DAEMON_HOST, SERVER_DAEMON_PORT, factory)


    def _leader(self, leader, eid):
        '''
            This method is to assign you the leadership and broadcast everyone
        '''
        if self._eid == eid:
            print 'fuck it i m the winner'
            self.cancel_wait_for_peers_callback()  # if leader wins due to no available peers, then cancel the wait for peers callback
            self.book.leader = leader
            size_value = random.randint(6, 10)
            self.secret_value = generate_secret(size_value)
            self._broadcast_winner(eid)

    def _bully(self):
        '''
            broadcast a re-election event
        '''
        print 'bullying then relection'
        self._broadcast_re_election()

    def _face_off(self, data=None):
        '''
            conflicting leaders will broadcast ledger
        '''
        self._broadcast_ledger()

    def notify_server(self):
        '''
            notifying the server as soon as there is a leader change
        '''
        if self.book.leader in self.book.peers:
            leader_host = self.book.peers[self.book.leader][0]
            leader_port = SERVER_DAEMON_PORT
            factory = ServerElectionFactory(leader_host, leader_port)
            reactor.connectTCP(SERVER_DAEMON_HOST, SERVER_DAEMON_PORT, factory)

    def reset(self):
        '''
            This resets all the values
        '''
        self.delay = self.initialDelay


#if __name__ == '__main__':
#    import random,os,sys
#    try:
#        from netifaces import interfaces, ifaddresses, AF_INET
#
#        def ip4_addresses():
#            ip_list = []
#            for interface in interfaces():
#                try:
#                    for link in ifaddresses(interface)[AF_INET]:
#                        ip_list.append(link['addr'])
#                except:
#                    pass
#            return ip_list
#        ips = ip4_addresses()
#        print ips
#        ip = input('Enter index of ip addr:')
#        timeuuid = time_uuid.TimeUUID.with_utcnow()
#        book = CommonlogBook(identity=timeuuid, state=0, ip = ips[ip-1])
#        reactor.listenMulticast(MCAST_ADDR[1], CommonroomProtocol(book), listenMultiple=True)
#        reactor.run()
#    except KeyboardInterrupt:
#        reactor.stop()
#        try:
#            sys.exit(0)
#        except SystemExit:
#            os._exit(0)